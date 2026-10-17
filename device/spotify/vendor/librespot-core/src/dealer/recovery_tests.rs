//! Local WebSocket fault injection; no Spotify account or Internet required.
use super::*;
use std::sync::atomic::AtomicUsize;
use tokio::{net::TcpListener, sync::oneshot, time::timeout};
use tokio_tungstenite::{WebSocketStream, accept_async};

const TEST_DEADLINE: Duration = Duration::from_secs(3);
const URI: &str = "hm://echomuse-test/";

async fn server() -> (TcpListener, Url) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = Url::parse(&format!("ws://{}/", listener.local_addr().unwrap())).unwrap();
    (listener, url)
}

async fn accept(listener: &TcpListener) -> WebSocketStream<tokio::net::TcpStream> {
    accept_async(listener.accept().await.unwrap().0)
        .await
        .unwrap()
}

async fn announce(socket: &mut WebSocketStream<tokio::net::TcpStream>) {
    socket
        .send(WsMessage::Text(
            format!(r#"{{"type":"message","uri":"{URI}"}}"#).into(),
        ))
        .await
        .unwrap();
}

async fn answer_pings(mut socket: WebSocketStream<tokio::net::TcpStream>) {
    while let Some(Ok(message)) = socket.next().await {
        if matches!(message, WsMessage::Close(_)) {
            break;
        }
        // tungstenite queues automatic pongs on read.
        if socket.flush().await.is_err() {
            break;
        }
    }
}

async fn launch<F, Fut>(builder: Builder, get_url: F) -> Dealer
where
    F: Fn() -> Fut + Send + 'static,
    Fut: Future<Output = GetUrlResult> + Send + 'static,
{
    timeout(TEST_DEADLINE, builder.launch(get_url, None))
        .await
        .expect("startup blocked on networking instead of starting a retryable task")
        .unwrap()
}

async fn expect_message(subscription: &mut Subscription) {
    assert_eq!(
        timeout(TEST_DEADLINE, subscription.next())
            .await
            .expect("Dealer stranded its live subscription instead of recovering")
            .expect("subscription unexpectedly ended")
            .uri,
        URI
    );
}

#[tokio::test]
async fn startup_retries_url_errors_and_timeouts() {
    url_errors_and_timeouts(false).await;
}

#[tokio::test]
async fn reconnect_retries_url_errors_and_timeouts() {
    url_errors_and_timeouts(true).await;
}

async fn url_errors_and_timeouts(after_disconnect: bool) {
    for stall in [false, true] {
        let (listener, url) = server().await;
        let attempts = Arc::new(AtomicUsize::new(0));
        let count = Arc::clone(&attempts);
        let (release, wait) = oneshot::channel();
        let peer = tokio::spawn(async move {
            if after_disconnect {
                let mut first = accept(&listener).await;
                announce(&mut first).await;
                // Keep it healthy until the assertion sees initial data.
                let heartbeat = tokio::spawn(answer_pings(first));
                wait.await.unwrap();
                heartbeat.abort(); // TCP EOF, then recover with new URL.
                let _ = heartbeat.await;
            }
            let mut recovered = accept(&listener).await;
            announce(&mut recovered).await;
            answer_pings(recovered).await;
        });
        let mut builder = Builder::new();
        let mut subscription = builder.subscribe(&[URI]).unwrap();
        let dealer = launch(builder, move || {
            let n = count.fetch_add(1, atomic::Ordering::SeqCst);
            let fail_at = usize::from(after_disconnect);
            let url = url.clone();
            async move {
                if n == fail_at {
                    if stall {
                        futures_util::future::pending::<()>().await;
                    }
                    Err(Error::unavailable("injected resolver/token failure"))
                } else {
                    Ok(url)
                }
            }
        })
        .await;
        if after_disconnect {
            expect_message(&mut subscription).await;
            release.send(()).unwrap();
        }
        expect_message(&mut subscription).await;
        assert_eq!(
            attempts.load(atomic::Ordering::SeqCst),
            if after_disconnect { 3 } else { 2 }
        );
        timeout(TEST_DEADLINE, dealer.close()).await.unwrap();
        peer.abort();
    }
}

#[tokio::test]
async fn startup_bounds_stalled_websocket_handshake() {
    stalled_handshake(false).await;
}

#[tokio::test]
async fn reconnect_bounds_stalled_websocket_handshake() {
    stalled_handshake(true).await;
}

async fn stalled_handshake(after_disconnect: bool) {
    let (listener, url) = server().await;
    let (release, wait) = oneshot::channel();
    let peer = tokio::spawn(async move {
        if after_disconnect {
            let mut first = accept(&listener).await;
            announce(&mut first).await;
            let heartbeat = tokio::spawn(answer_pings(first));
            wait.await.unwrap();
            heartbeat.abort();
            let _ = heartbeat.await;
        }
        // Accept TCP but withhold the WebSocket handshake indefinitely.
        let blackhole = listener.accept().await.unwrap().0;
        let mut recovered = accept(&listener).await;
        announce(&mut recovered).await;
        drop(blackhole);
        answer_pings(recovered).await;
    });
    let mut builder = Builder::new();
    let mut subscription = builder.subscribe(&[URI]).unwrap();
    let dealer = launch(builder, move || {
        let url = url.clone();
        async move { Ok(url) }
    })
    .await;
    if after_disconnect {
        expect_message(&mut subscription).await;
        release.send(()).unwrap();
    }
    expect_message(&mut subscription).await;
    timeout(TEST_DEADLINE, dealer.close()).await.unwrap();
    peer.abort();
}

#[tokio::test]
async fn missing_pong_recovers_the_same_live_subscription() {
    let (listener, url) = server().await;
    let peer = tokio::spawn(async move {
        // Reproduce the log's "Websocket peer does not respond" without
        // closing TCP: accept WebSocket, then never read/respond to pings.
        let blackhole = accept(&listener).await;
        let mut recovered = accept(&listener).await;
        announce(&mut recovered).await;
        drop(blackhole);
        answer_pings(recovered).await;
    });
    let mut builder = Builder::new();
    let mut subscription = builder.subscribe(&[URI]).unwrap();
    let dealer = launch(builder, move || {
        let url = url.clone();
        async move { Ok(url) }
    })
    .await;
    expect_message(&mut subscription).await;
    timeout(TEST_DEADLINE, dealer.close()).await.unwrap();
    peer.abort();
}

#[tokio::test]
async fn healthy_idle_connection_is_not_periodically_restarted() {
    let (listener, url) = server().await;
    let attempts = Arc::new(AtomicUsize::new(0));
    let count = Arc::clone(&attempts);
    let peer = tokio::spawn(async move {
        let mut socket = accept(&listener).await;
        announce(&mut socket).await;
        answer_pings(socket).await;
    });
    let mut builder = Builder::new();
    let mut subscription = builder.subscribe(&[URI]).unwrap();
    let dealer = launch(builder, move || {
        count.fetch_add(1, atomic::Ordering::SeqCst);
        let url = url.clone();
        async move { Ok(url) }
    })
    .await;
    expect_message(&mut subscription).await;
    tokio::time::sleep(PING_INTERVAL * 5).await;
    assert_eq!(attempts.load(atomic::Ordering::SeqCst), 1);
    timeout(TEST_DEADLINE, dealer.close()).await.unwrap();
    peer.abort();
}

#[tokio::test]
async fn shutdown_cancels_url_lookup_handshake_and_retry_sleep() {
    for stage in ["lookup", "handshake", "backoff"] {
        let (listener, url) = server().await;
        let peer = tokio::spawn(async move {
            let socket = listener.accept().await.unwrap().0;
            futures_util::future::pending::<()>().await;
            drop(socket);
        });
        let dealer = launch(Builder::new(), move || {
            let url = url.clone();
            async move {
                match stage {
                    "lookup" => futures_util::future::pending().await,
                    "backoff" => Err(Error::unavailable("offline")),
                    _ => Ok(url),
                }
            }
        })
        .await;
        tokio::time::sleep(Duration::from_millis(10)).await;
        timeout(Duration::from_millis(100), dealer.close())
            .await
            .expect("shutdown waited for networking/retry deadline");
        peer.abort();
    }
}

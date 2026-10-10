//! Feasibility probe for on-device Spotify Connect.
//!
//! Run this on a real Echo BEFORE anything is built on top of librespot.
//! It answers, in order, the three questions that decide whether the
//! device-side design is possible at all, and it answers each one
//! separately so a failure names its own cause:
//!
//!   1. **Does a static musl armv7 binary execute here?**  Reaching the
//!      first line of output is the whole of that answer. A kernel that
//!      refuses the binary produces nothing, which is why step 0 prints
//!      before it does any work — silence and a failed network are
//!      otherwise indistinguishable, the same reason emOS's init is a
//!      static C binary (emos/README.md).
//!
//!   2. **Does DNS resolve?**  emOS's init writes /etc/resolv.conf pointing
//!      at the default gateway and musl reads it; bionic would not, and on
//!      FireOS the file does not exist. The probe prints what it found in
//!      that file before it resolves, so "no nameserver configured" and
//!      "nameserver did not answer" are different findings.
//!
//!   3. **Does a rustls handshake to Spotify succeed?**  This exercises
//!      `ring`'s armv7 assembly and the compiled-in Mozilla roots. It is
//!      also where a wrong clock shows up: the Echo boots reading 2010 and
//!      the controller sets the time on the `ack`, so a handshake run
//!      before that fails certificate validation. That failure is reported
//!      AS a clock problem rather than as a TLS problem, because the two
//!      want completely different investigations.
//!
//! Exit status is 0 only if every step passed. Nothing here writes to the
//! filesystem, touches the mixer, or opens a listening socket.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Spotify's access-point resolver. Chosen deliberately as the probe's
/// target: it is the first host librespot itself contacts, it is plain
/// HTTPS with no credentials, and its answer is small. Hitting the real
/// endpoint means a pass here is evidence about the actual path the daemon
/// will take rather than about example.com.
const APRESOLVE_HOST: &str = "apresolve.spotify.com";
const APRESOLVE_PATH: &str = "/?type=accesspoint";

/// Connect and read timeouts. Short enough that a probe against a dead
/// network finishes while someone is still watching it, long enough not to
/// fail a healthy Echo on a loaded 2.4GHz link — this fleet has measured
/// multi-second TCP stalls (controller/CLAUDE.md, the RTT investigation).
const NET_TIMEOUT: Duration = Duration::from_secs(15);

/// A clock below this means the controller has not set the time yet, so
/// certificate validation cannot possibly pass. 2020-01-01. The point of a
/// floor rather than an exact check is that any plausible real date is
/// above it and the Echo's own boot default (2010) is far below.
const CLOCK_FLOOR_UNIX: u64 = 1_577_836_800;

fn main() {
    // Step 0 prints before any work, because reaching it IS question 1.
    println!("em-spotify probe");
    println!("  librespot       : {}", librespot_core::version::SEMVER);
    println!("  target          : {}", std::env::consts::ARCH);
    println!("  pointer width   : {} bits", usize::BITS);

    let mut failures: Vec<String> = Vec::new();

    match check_clock() {
        Ok(desc) => println!("  clock           : {desc}"),
        Err(e) => {
            println!("  clock           : FAIL {e}");
            failures.push(format!("clock: {e}"));
        }
    }

    report_resolv_conf();

    let addr = match resolve(APRESOLVE_HOST) {
        Ok((addr, elapsed, all)) => {
            println!(
                "  dns             : ok {APRESOLVE_HOST} -> {} ({} address(es), {} ms)",
                addr,
                all,
                elapsed.as_millis()
            );
            Some(addr)
        }
        Err(e) => {
            println!("  dns             : FAIL {e}");
            failures.push(format!("dns: {e}"));
            None
        }
    };

    if let Some(addr) = addr {
        match tls_get(addr) {
            Ok(desc) => println!("  tls + https     : ok {desc}"),
            Err(e) => {
                println!("  tls + https     : FAIL {e}");
                failures.push(format!("tls: {e}"));
            }
        }
    } else {
        println!("  tls + https     : skipped (no address to connect to)");
        failures.push("tls: skipped, DNS did not resolve".to_string());
    }

    println!();
    if failures.is_empty() {
        println!("PASS — this device can run librespot natively.");
    } else {
        println!("FAIL — {} check(s) failed:", failures.len());
        for f in &failures {
            println!("  - {f}");
        }
        std::process::exit(1);
    }
}

/// Whether the system clock is plausible enough for certificate validation.
///
/// Reported as its own step so the TLS failure it causes is not read as a
/// broken trust store. On an Echo this is a sequencing fact rather than a
/// fault: the controller sends `time_ms` on the `ack`, so the clock is
/// correct once the link is up and wrong before it.
fn check_clock() -> Result<String, String> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "system clock is before the unix epoch".to_string())?;
    let secs = now.as_secs();
    if secs < CLOCK_FLOOR_UNIX {
        return Err(format!(
            "unix {secs} is implausibly early — the controller has not set the \
             time yet, so TLS cannot validate a certificate. Start librespot \
             only after the first config push."
        ));
    }
    Ok(format!("unix {secs} (plausible)"))
}

/// Print the resolver configuration musl will use.
///
/// A missing file and an empty one are reported differently: the first is
/// the FireOS case (no /etc/resolv.conf at all, /etc symlinked into
/// read-only /system/etc) and the second would mean emOS's init ran
/// `write_resolv_conf` and found no default gateway. Neither is a DNS
/// failure yet — the resolution step below is what settles that — so this
/// step never fails the probe on its own.
fn report_resolv_conf() {
    match std::fs::read_to_string("/etc/resolv.conf") {
        Ok(body) => {
            let servers: Vec<&str> = body
                .lines()
                .map(str::trim)
                .filter(|l| l.starts_with("nameserver"))
                .collect();
            if servers.is_empty() {
                println!("  /etc/resolv.conf: present but names no nameserver");
            } else {
                println!("  /etc/resolv.conf: {}", servers.join(", "));
            }
        }
        Err(e) => println!("  /etc/resolv.conf: unreadable ({e})"),
    }
}

/// Resolve a hostname to its first address, timing the lookup.
///
/// Returns the address actually chosen plus how many were offered, because
/// a host resolving to one address on a network that normally offers
/// several is itself worth seeing.
fn resolve(host: &str) -> Result<(SocketAddr, Duration, usize), String> {
    let started = Instant::now();
    let addrs: Vec<SocketAddr> = (host, 443u16)
        .to_socket_addrs()
        .map_err(|e| format!("{host} did not resolve: {e}"))?
        .collect();
    let elapsed = started.elapsed();
    let first = addrs
        .first()
        .copied()
        .ok_or_else(|| format!("{host} resolved to no addresses"))?;
    Ok((first, elapsed, addrs.len()))
}

/// Handshake to Spotify over TLS and issue one plain HTTP/1.1 GET.
///
/// Deliberately hand-rolled rather than done through an HTTP client crate:
/// the thing under test is rustls plus `ring` on this CPU, and a smaller
/// dependency surface means a failure here points at the stack we care
/// about instead of at somebody's connection pool.
fn tls_get(addr: SocketAddr) -> Result<String, String> {
    let roots = rustls::RootCertStore {
        roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
    };
    let config = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    let server_name = rustls_pki_types::ServerName::try_from(APRESOLVE_HOST)
        .map_err(|e| format!("{APRESOLVE_HOST} is not a valid server name: {e}"))?
        .to_owned();
    let mut conn = rustls::ClientConnection::new(Arc::new(config), server_name)
        .map_err(|e| format!("could not start a TLS session: {e}"))?;

    let started = Instant::now();
    let mut sock = TcpStream::connect_timeout(&addr, NET_TIMEOUT)
        .map_err(|e| format!("could not connect to {addr}: {e}"))?;
    sock.set_read_timeout(Some(NET_TIMEOUT)).ok();
    sock.set_write_timeout(Some(NET_TIMEOUT)).ok();
    let connected = started.elapsed();

    let mut tls = rustls::Stream::new(&mut conn, &mut sock);
    let request = format!(
        "GET {APRESOLVE_PATH} HTTP/1.1\r\n\
         Host: {APRESOLVE_HOST}\r\n\
         User-Agent: em-spotify-probe\r\n\
         Accept: */*\r\n\
         Connection: close\r\n\r\n"
    );
    tls.write_all(request.as_bytes())
        .map_err(|e| describe_tls_error("writing the request", e))?;

    let mut body = Vec::new();
    // read_to_end over a `Connection: close` response: the server closes,
    // which arrives as a clean EOF. An UnexpectedEof from rustls means the
    // peer went away mid-record and is reported as such rather than as a
    // short read.
    match tls.read_to_end(&mut body) {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof && !body.is_empty() => {
            // Some Spotify edges close without a TLS close_notify. The
            // bytes already read are still the answer, so this is not a
            // failure — but say that it happened.
            println!("  (peer closed without close_notify; response was complete)");
        }
        Err(e) => return Err(describe_tls_error("reading the response", e)),
    }

    let total = started.elapsed();
    let text = String::from_utf8_lossy(&body);
    let status = text
        .lines()
        .next()
        .unwrap_or("<no status line>")
        .trim()
        .to_string();
    let protocol = conn
        .protocol_version()
        .map(|v| format!("{v:?}"))
        .unwrap_or_else(|| "unknown".to_string());
    let suite = conn
        .negotiated_cipher_suite()
        .map(|s| format!("{:?}", s.suite()))
        .unwrap_or_else(|| "unknown".to_string());

    Ok(format!(
        "{status} — {protocol}, {suite}, {} bytes (tcp {} ms, total {} ms)",
        body.len(),
        connected.as_millis(),
        total.as_millis()
    ))
}

/// Turn an io error from the TLS stream into something that names a cause.
///
/// Certificate validation failures and a dead network both surface as an
/// io error here, and they are the two outcomes most likely on a device
/// whose clock the controller has not set yet. Naming the clock explicitly
/// is what stops an hour going into the trust store.
fn describe_tls_error(during: &str, e: std::io::Error) -> String {
    let text = e.to_string();
    let lower = text.to_ascii_lowercase();
    if lower.contains("expired")
        || lower.contains("not valid yet")
        || lower.contains("notvalidyet")
        || lower.contains("certificate")
    {
        return format!(
            "{during}: {text} — check the clock first; an Echo that the \
             controller has not told the time to cannot validate any \
             certificate"
        );
    }
    format!("{during}: {text}")
}

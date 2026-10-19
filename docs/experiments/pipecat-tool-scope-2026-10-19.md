# Weather tools per composed voice turn

Ordinary composed voice pipelines now keep local command handling first and
select a smaller advertised tool set for narrowly recognized, standalone
English weather questions that reach the LLM. This applies to Cerebras,
Groq and other composed providers without a separate classifier model call.

The smaller set contains configured weather/forecast tools, the continuation
tool if configured, and `EchoMuseExpandTools`. Expansion performs no home
action: it restores the original configured/allowlisted tool set and lets the
LLM continue. Tool names, descriptions, argument schemas, instructions and
conversation history are preserved. Expansion can require another model call.

Fresh ambiguous requests, follow-ups such as “what about tomorrow”, combined
commands, unsupported languages, absent weather tools, explicit tool choices,
direct callable tools and provider-specific custom tools retain the full set.
Each fresh user turn restores its own selection. Tool-result continuations do
not reset an expanded turn; stale expansion callbacks cannot change a new one.
Flow graphs, Gemini Live and the separate HA Assist text HTTP route are unchanged.

Read-only discovery against the actual HA MCP server found 34 tools. Including
the voice continuation tool, the real OpenAI-compatible request tool list
shrank from 35 entries to 3: `assist__script__get_weather`,
`EchoMuseRequestContinuation`, and `EchoMuseExpandTools`.
Compact serialized tool definitions measured approximately **3,233 -> 222**
tokens using `cl100k_base`. These are tool-definition estimates, not actual
Cerebras billing tokens or whole-request totals. Weather results, model
framing, the user prompt and history still contribute input tokens.

Local sentences remain preferable for deterministic queries. HA already
answered “what's the weather like” with its built-in local agent during this
audit. However, it also accepted “what's the weather forecast tomorrow” and
returned the same current-conditions speech. Adding future-weather sentences
must include an appropriate forecast handler; recognition alone is not proof
that the answer refers to the requested time. “Will it rain tomorrow” returned
`no_intent_match`, making it an example of a potential scoped LLM fallback.

Validation includes real Pipecat pipelines and OpenAI SDK streaming loops with
mock Cerebras/Groq responses: initial narrow request, expansion call, restored
tools, and resumed inference. Turn isolation, retained history, allowlisted
tool preservation, schema identity, language/ambiguity fallback and stale
callbacks are covered. No live Cerebras inference or production configuration
change was performed. The relevant suites passed 98 tests.

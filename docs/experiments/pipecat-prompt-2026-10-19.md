# Pipecat composed prompt audit

The composed runtime supplied `_effective_instructions(flow)` both through
the LLM service's settings and as an initial developer context message.
Pipecat 1.4's OpenAI-compatible adapter prepends the settings instruction
without removing the developer message. Groq therefore received both.

Ordinary composed pipelines now supply the policy through service settings
once. User-configured instructions, greetings, tools and conversation history
remain present. Flow graphs retain their existing context role lifecycle;
Gemini Live retains the original EchoMuse prompt and realtime wiring.

The composed EchoMuse policy is also shorter and includes scheduler-specific
instructions only when those tools are enabled. It retains required
clarifications/continuation, short confirmations, language selection, room
targeting, one-item stop precedence and `left_running` protection, stop fallback,
active-only snooze/cancel, acknowledged scheduling, timezone clarification,
resume versus titled music, no fabricated media IDs or repeated playback,
exact area names, bounded context retries and percentage volume semantics.

Reference token estimates (`cl100k_base`, **not** Groq's model tokenizer),
using Bedroom, English and enabled EchoMuse scheduling:

| Content | Before | After |
| --- | ---: | ---: |
| One EchoMuse policy | 829 | 396 |
| EchoMuse policy across the composed request | 1,658 | 396 |

This saves about 1,262 reference tokens, plus removal of the duplicated
user-configured base instructions and generated conversation-end hints.
It does not establish an exact total or latency improvement for Groq.

Read-only discovery against the real HA MCP endpoint found 34 tools. Their
compact serialized schemas alone measured approximately 2,923 reference
tokens, excluding EchoMuse's additional session tools and provider framing.
Their names, descriptions and argument schemas were left intact. They explain
much of the remaining input; removing tools would change available behavior.

Validation: Pipecat's actual OpenAI-compatible adapter emits one full system
policy while retaining the user greeting and conversation. Tests also check
the preserved flow-graph context and scheduler capability gating. Together
with the existing adapter/routing and runtime regressions, 70 tests passed.
No Groq model behavior evaluation or live production prompt update was run.

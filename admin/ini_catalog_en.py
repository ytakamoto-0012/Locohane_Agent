"""config.ini の説明（コメント）の英訳カタログ（設定ダッシュボードの英語表示用）。

日本語の正本は config.ini のコメント。config.ini のキーを追加・説明を変更したら、ここも合わせて更新する
（tests/test_admin_ini_catalog_en.py が全キーの訳があることを確かめる）。
"""

# グループ見出し（config.ini の "# --- xxx ---" 行）の英訳。キーは日本語の見出しそのもの。
HEADINGS: dict[str, str] = {
    'メインエージェント用の接続先': 'Endpoints for the main agent',
    'サブエージェント（dispatch_agent）用の接続先': 'Endpoints for sub-agents (dispatch_agent)',
    '生成パラメータ（main/sub共通）': 'Generation parameters (shared by main/sub)',
    "DRY (Don't Repeat Yourself) サンプラー（llama.cpp拡張、vLLMへは送らない）": "DRY (Don't Repeat Yourself) sampler (llama.cpp extension; not sent to vLLM)",
    'round_robin / priority_failoverの/slots空き状況確認（provider="llama_cpp"の接続先のみ対象。キー名は互換性のため round_robin_ のまま）': 'Checking free /slots for round_robin / priority_failover (only endpoints with provider="llama_cpp"; the key names keep round_robin_ for compatibility)',
    '接続エラー時の自動リトライ（メインエージェント）': 'Automatic retry on connection errors (main agent)',
    '並列数ガード': 'Parallelism guard',
    'メインエージェントのトークンガード': 'Token guard of the main agent',
    'トークン使用量ガード': 'Token usage guard',
    '空応答ガード': 'Empty response guard',
    'dispatch_agent のバックグラウンドジョブ機構用設定': 'Settings for the background job mechanism of dispatch_agent',
    'メインエージェントの基本運用に必須のツール: max_calls=-1（無制限）で登録': "Tools essential to the main agent's basic operation: registered with max_calls=-1 (unlimited)",
    '判定ルール（最後のユーザー発言以降を対象に、ルール1→5の順で最初に当てはまったものを使う）': 'Decision rules (looking at everything after the last user message, the first of rules 1 -> 5 that applies is used)',
    '入力欄（Composer）の貼り付け・文字数上限': 'Pasting and character limit of the input box (Composer)',
    'トークン使用量カードの強調表示': 'Highlighting of the token usage card',
    '管理アシスタント（ダッシュボードの「AIに相談」）': 'Admin assistant ("Ask AI" on the dashboard)',
    'データのバックアップ（設定ダッシュボードの「データバックアップ」画面）': 'Data backup (the "Data backup" screen of the settings dashboard)',
}

# キーの説明の英訳。キーは "section.key"。
DESCRIPTIONS: dict[str, str] = {
    'llm.main_url': (
        'OpenAI-compatible endpoint of llama.cpp server (llama-server). If several are given, one is chosen per call according to main_routing_strategy (if only one, it is always used). One endpoint = a dict {"base_url":..., "api_key":..., "model":...}. Specify it as a JSON/Python-style list (square brackets, multiple lines OK, trailing comma allowed;\n'
        'the same notation as [thinking_loop_guard].nudge_messages).\n'
        'base_url: e.g. http://localhost:8080/v1 (e.g. llama-server --host 127.0.0.1 --port 8080)\n'
        'api_key : llama.cpp needs no authentication, so a dummy value is fine (put something in, because the client rejects an empty value). Defaults to "dummy-not-used" when omitted.\n'
        'model   : switch it to match --model / --alias of llama-server.\n'
        'start/end: the time range in which this endpoint can be used (optional). The unit is hours (H); give minutes as a decimal (e.g. 9.5 = 9:30, 22.25 = 22:15).\n'
        'Always specify start/end as a pair (specifying only one is not allowed).\n'
        'If both are omitted, it can be used at any time. If start > end, the range crosses midnight (e.g. start=22, end=6 means 22:00 to 6:00 the next day).\n'
        'The list as a whole needs at least one "always available" endpoint with both start/end omitted (so that there is no moment that no time range covers). At moments when only out-of-range endpoints exist, one is chosen from the always-available endpoints (or from all endpoints if there are none).\n'
        'provider: "openai_compatible" (default, can be omitted) / "llama_cpp" / "vllm".\n'
        'The kind of inference server. The names of the extension parameters sent and the thinking field names are adjusted to the server (see src/llm/dialect.py).\n'
        'For "vllm", repeat_penalty below is sent as repetition_penalty and reasoning_budget as thinking_token_budget, and dry_* / reasoning_format / reasoning_budget_message, which vLLM does not have, are not sent (a WARNING is logged once the first time after startup).\n'
        '"openai_compatible" sends the same parameters as llama-server.\n'
        'Also, with the round_robin / priority_failover strategies, "llama_cpp" makes the app check for free slots with GET /slots before choosing the endpoint; if none are free it skips to the next candidate, and if all candidates are full it waits round_robin_busy_poll_interval_seconds seconds and checks again (see below). Between choosing it and sending the first request, the free slot is reserved so that other calls being chosen at the same time do not take it. This is an API specific to llama.cpp server (works only when the --slots startup option is enabled), so do not specify it for other OpenAI-compatible servers (in that case no check is made and the endpoint is chosen immediately as before).\n'
        '{"base_url": "http://localhost:12430/v1", "api_key": "dummy-not-used", "model": "QWEN3.6_35B-A3B"},\n'
        '{"base_url": "http://localhost:12431/v1", "api_key": "dummy-not-used", "model": "QWEN3.6_35B-A3B", "start": 22, "end": 6, "provider": "llama_cpp"},'
    ),
    'llm.main_routing_strategy': (
        'How to choose when there are several main_url entries.\n'
        'round_robin      : rotate in order on each call (default). Endpoints with provider="llama_cpp" are checked for free slots with GET /slots before being chosen; if none are free, the app skips to the next one, and if all candidates are full it waits until a slot frees up (see round_robin_busy_poll_interval_seconds).\n'
        'Endpoints with provider="openai_compatible" are not checked and are chosen immediately as before.\n'
        'random            : choose randomly on each call\n'
        'priority_failover : always use the first one, and switch temporarily to the next only when a communication error is detected. Like round_robin, endpoints with provider="llama_cpp" are checked for free slots with GET /slots before being chosen; if none are free, the app skips to the next one, and if all candidates are full it waits until a slot frees up.\n'
        'Endpoints with provider="openai_compatible" are not checked and are chosen immediately as before.'
    ),
    'llm.sub_url': (
        'Same format and default as main_url. Change this when you want to route to a llama-server instance / model different from the main agent.\n'
        'If not specified (no key, or an empty value), main_url is used as is.\n'
        'If a model name is specified with the frontmatter model of agents/*.md (or the model argument of dispatch_agent), only endpoints whose "model" here matches are candidates, chosen with sub_routing_strategy. If no endpoint matches (or all are out of their time range), sub_default_model below is used, and if that cannot be used either, the specification is ignored.'
    ),
    'llm.sub_routing_strategy': (
        'How to choose when there are several sub_url entries. The choices (round_robin / random / priority_failover) and behavior are the same as main_routing_strategy.\n'
        'Ignored when sub_url is not specified (main_url is used as is); in that case the endpoint the main agent used most recently in the same session is used as is (so as not to scatter the KV cache).'
    ),
    'llm.sub_default_model': (
        'The model name used when a sub-agent has no model specified (the frontmatter model of agents/*.md, the model argument of dispatch_agent).\n'
        'Only endpoints whose sub_url "model" matches are candidates, chosen with sub_routing_strategy (case-insensitive).\n'
        'If you mix lightweight and high-performance models in sub_url and leave this unspecified, delegations without a model are sent randomly to either.\n'
        'This model is also used when the model specified above does not match (or all matches are out of their time range).\n'
        'If empty, endpoints are chosen without looking at the model name as before. Ignored when no endpoint matches (or all are out of their time range) (a wrong name logs a WARNING once to app.log at startup).\n'
        'Note: also effective when sub_url is empty so that the main endpoints are inherited. If the main agent is using an endpoint of a different model, sub-agents do not inherit it and\n'
        'choose from the endpoints of main_url with the sub_default_model model (leave this empty if you want them to use the same endpoint as the main agent).'
    ),
    'llm.temperature': (
        'Randomness of generation (0.0 or more). Keep it low to prioritize the stability of tool calls.'
    ),
    'llm.top_p': (
        'Threshold for sampling only from the top cumulative probability (0 to 1). If empty, not specified (left to the llama-server default).'
    ),
    'llm.top_k': (
        'Sample only from the top k candidates (llama.cpp / vLLM extension; not in the standard OpenAI API). If empty, not specified.'
    ),
    'llm.repeat_penalty': (
        'Coefficient that suppresses recently generated tokens from reappearing (llama.cpp extension; 1.0 = disabled, around 1.1 suppresses lightly).\n'
        'Sent as repetition_penalty to endpoints with provider="vllm". If empty, not specified. It directly helps suppress loops that repeat the same wording inside thinking (<think> blocks), but it trades off against the accuracy of generating repetitive strings such as file paths, so loop suppression itself is left to thinking_loop_guard (detection + retry on the app side).'
    ),
    'llm.frequency_penalty': (
        'Uniform penalty on all tokens that have appeared (standard OpenAI API; 0.0 = disabled). If empty, not specified.'
    ),
    'llm.presence_penalty': (
        'Uniform penalty on tokens that have appeared even once (standard OpenAI API; 0.0 = disabled). If empty, not specified.'
    ),
    'llm.max_tokens': (
        'Maximum number of tokens generated per request. If empty, unlimited (left to the llama-server default).\n'
        'It also acts as a brake for cases where runaway thinking keeps a response from ever returning.'
    ),
    'llm.dry_multiplier': (
        'Works strongly on repetition in units of phrases longer than repeat_penalty handles (repeating the same sentence inside thinking, etc.). 0.0 or empty disables it. It trades off against the accuracy of generating repetitive strings such as file paths, so countermeasures against runaways are left to [thinking_loop_guard] (detection + retry on the app side) and [llm].max_tokens (the final brake).'
    ),
    'llm.dry_base': (
        'Exponential growth rate of the penalty with respect to the repetition length (default equivalent to 1.75). If empty, not specified.'
    ),
    'llm.dry_allowed_length': (
        'Repetitions of this many characters or fewer are allowed (default equivalent to 2). If empty, not specified.'
    ),
    'llm.dry_penalty_last_n': (
        'Number of tokens to look back for repetition detection (-1 for the whole context; default equivalent to -1). If empty, not specified.'
    ),
    'llm.dry_sequence_breakers': (
        'Separator characters that reset repetition detection (several in list form, e.g. ["\\n", ":", "\\"", "*"]).\n'
        'An empty list [] leaves it to the llama-server default (newline, :, ", *).'
    ),
    'llm.enable_thinking': (
        'Turns the thinking (reasoning, <think> block) mode of Qwen3-family models on/off (common to llama.cpp / vLLM; sent via chat_template_kwargs in extra_body).\n'
        'If empty, not specified and left to the model / llama-server default. false turns reasoning off (responses get faster, but accuracy on complex tasks may drop).'
    ),
    'llm.reasoning_format': (
        'How thinking tags (<think>...</think>) are handled (llama.cpp extension, sent via extra_body; the value corresponds to --reasoning-format FORMAT when starting llama-server).\n'
        'Not sent to vLLM (vLLM decides it with --reasoning-parser at startup).\n'
        'none            : do not parse; leave the <think> tags in message.content\n'
        "deepseek        : separate the thinking part into message.reasoning_content (this app's ChatLlamaCpp picks up reasoning_content / reasoning and uses it for the thinking display in the UI;\n"
        'see the ChatLlamaCpp docstring in src/llm/chat_model.py)\n'
        'deepseek-legacy : keep the <think> tags in message.content and also put it in message.reasoning_content (compatible with the old DeepSeek API)\n'
        "If empty, not specified (left to llama-server's default auto = decided automatically from the template)."
    ),
    'llm.reasoning_budget': (
        'Upper limit on the number of tokens usable for thinking (sent via extra_body;\n'
        'the value corresponds to --reasoning-budget N when starting llama-server).\n'
        '-1 = unlimited, N>0 = limit to that number of tokens.\n'
        'Sent to llama-server as reasoning_budget_tokens and to vLLM as thinking_token_budget (-1 is not sent). llama-server ignores 0 in requests (vLLM turns thinking off with 0), so to stop thinking on either, use reasoning_effort = none or enable_thinking = false.\n'
        'Note: when thinking is cut off by this limit, the rest of the thinking may leak into the body (content) (3 times out of 6 with llama-server b10437 / Qwen3.8; with vLLM 0.30.0 / Qwen3.5-9B-AWQ almost all thinking after the cutoff appeared in the body). Thinking leaked into the body is treated as the answer as is, so if you set this, use a sufficiently large value.\n'
        "If empty, not specified (left to llama-server's default -1 = unlimited)."
    ),
    'llm.reasoning_budget_message': (
        'Message inserted just before the end-of-thinking tag when reasoning_budget above is used up (llama.cpp extension, sent via extra_body; the value corresponds to --reasoning-budget-message MESSAGE when starting llama-server). If empty, nothing is inserted (llama-server default). Not sent to vLLM.'
    ),
    'llm.reasoning_effort': (
        'Reasoning effort level passed to the chat template (common to llama.cpp / vLLM, sent via extra_body; the value corresponds to --reasoning-effort LEVEL when starting llama-server).\n'
        'none / default / minimal / low / medium / high / xhigh / max\n'
        'none disables thinking (so that it works even when the template does not read reasoning_effort, enable_thinking=false is also sent, taking precedence over enable_thinking = true). default is treated the same as "not sent".\n'
        "The accepted values depend on the model's template, and unsupported values make the request fail (e.g. Qwen3.8 accepts only low / medium / high / xhigh; minimal / max give HTTP 500). If empty, not specified (left to the server default)."
    ),
    'llm.reasoning_preserve': (
        'Whether to keep the thinking of past assistant messages in the conversation history sent (equivalent to --reasoning-preserve / --no-reasoning-preserve when starting llama-server; both preserve_reasoning and preserve_thinking are put in chat_template_kwargs of extra_body). If true, the thinking of past responses kept by this app is also included in requests (context consumption increases). Only thinking after the last user message (within the current tool loop) is included; thinking from earlier user turns is not sent.\n'
        'The field name follows the provider (llama_cpp: reasoning_content,\n'
        'vllm: reasoning, openai_compatible: both). If [context_trim].trim_ai_messages is enabled, old thinking outside the kept range is not sent when trimming kicks in.\n'
        'It has no effect on models whose template does not support it (e.g. Qwen3.5 always discards thinking before the last user message regardless of preserve_thinking; Qwen3.8 supports it).\n'
        'If empty, not specified (left to the server default).'
    ),
    'llm.track_token_usage': (
        'Whether to get token usage (input/output/total). true enables stream_usage=True of ChatOpenAI, and token counts are shown in the Chainlit UI and eval results. Set false only if llama-server does not support the stream_options.include_usage extension and errors out.'
    ),
    'llm.request_timeout_seconds': (
        '==== Relationship between request_timeout_seconds and stream_chunk_timeout_seconds ====\n'
        'These two are "nested" in the implementation. langchain_openai\'s stream_chunk_timeout (stream_chunk_timeout_seconds below) just wraps, with asyncio.wait_for, the wait on httpx\'s read timeout (request_timeout_seconds below) inside it; they are not monitored independently.\n'
        'Outer: asyncio.wait_for(..., timeout=stream_chunk_timeout_seconds)\n'
        'Inner: httpx read (timeout=request_timeout_seconds)\n'
        'So during the period in which the server has not returned a single byte yet (= the wait from the end of prompt processing (prefill) until the first chunk arrives), both clocks run at the same time. In other words, the time you can actually wait is\n'
        'effective timeout = min(request_timeout_seconds, stream_chunk_timeout_seconds)\n'
        'and raising only one of them does not help if the other is small; the smaller one fires first. Sending long prompts of tens of thousands of tokens makes prefill take longer, so both need to be sufficiently larger than the prefill time.\n'
        'Note that it is not "raising the larger one makes it safe" but "the smaller one is the limit".\n'
        '\n'
        'Use evals/analyze_timing.py to calculate recommended values from measurements (the formulas in evals/analyze_timing.py; if the numbers change, that file is authoritative):\n'
        'request_timeout_seconds      recommended = ceil(max_llm_total_seconds × 1.5) (minimum 60 seconds)\n'
        'stream_chunk_timeout_seconds recommended = ceil(max_stream_chunk_gap_seconds × 2.0) (minimum 30 seconds)\n'
        '\n'
        'Timeout in seconds for waiting for responses from the LLM server (read/write/pool; connect is fixed separately at 10 seconds). It is the "inner" one in the nested structure above = the lowest-level timeout at the httpx level, measuring the interval at which raw bytes arrive at the socket. The read timeout is the upper limit of idle time between chunks and is reset every time a chunk arrives as long as generation continues, so it does not prevent normal long streaming (but note, as above, that the clock also runs while waiting for prefill before streaming starts).\n'
        'It is a value for detecting abnormal situations such as the next request hanging forever because the client was left broken by a failure to clean up a stream.'
    ),
    'llm.stream_chunk_timeout_seconds': (
        'Timeout in seconds when no chunk arrives for a certain time during streaming (langchain_openai\'s stream_chunk_timeout). It is the "outer" one in the nested structure above = at the application layer one level above request_timeout_seconds, measuring the interval at which parsed chunks (one SSE event) arrive. It is the upper limit of "the interval between chunks"; in environments where prompt processing (prefill) of a large context takes time (low CPU/GPU performance, CPU offloading with n-cpu-moe, etc.), it tends to time out by exceeding this many seconds before generation starts. As described above, request_timeout_seconds is also in effect at the same time, so raising only this one is pointless if request_timeout_seconds is smaller. The library default is 120 seconds. Setting this to 0 disables wait_for in _astream_with_chunk_timeout in langchain_openai, and the creation of an asyncio.Task per chunk (running it as a separate task) stops altogether (request_timeout_seconds guarantees the read timeout, so 0 does not hurt safety).'
    ),
    'llm.max_concurrent_requests': (
        'Upper limit on the number of concurrent requests to llama-server. 1 or more: guarded with asyncio.Semaphore(N) (default 1 = fully serialized). 0 or less: guard disabled (unlimited).\n'
        'One pool shared by all sessions and the main/sub agents combined. A slot is occupied only during one generation by the LLM (from the start to the end of streaming). Even with several endpoints (main_url/sub_url), it is counted in total, not per endpoint.\n'
        'It is safest to match the --parallel value of the llama-server in operation (the total if there are several machines).\n'
        '\n'
        'Relationship with [graph].max_parallel / [subagent].max_parallel:\n'
        'Those are "how many tools/sub-agents to run at the same time within one session", and this is "how many of them actually make the LLM generate at the same time". It is a two-stage checkpoint.\n'
        'Example: [subagent].max_parallel=4 and this value=2\n'
        '- All 4 sub-agents start (all 4 show as "running" in the UI too)\n'
        '- Up to 2 LLM generations at the same time. The other 2 wait just before calling the LLM\n'
        '- Tool execution (reading files, etc.) does not use LLM slots, so all 4 proceed at the same time. So it does happen that one sub-agent uses an LLM slot while another sub-agent is running a tool\n'
        '- Waiting time is not counted toward the timeouts (request_timeout_seconds / stream_chunk_timeout_seconds). Measurement starts after an LLM slot is obtained\n'
        'So making max_parallel > this value causes no errors or slot overflows. It only means waiting for the LLM. Tasks with a lot of tool execution get somewhat faster, but tasks centered on LLM generation do not get faster than this value allows.\n'
        'Conversely, if max_parallel < this value, a single session cannot use up all the slots (other sessions use the remaining slots).'
    ),
    'llm.round_robin_slots_probe_timeout_seconds': (
        'Timeout in seconds of the GET /slots query itself that main_routing_strategy/sub_routing_strategy=round_robin/priority_failover sends before choosing an endpoint with provider="llama_cpp".\n'
        'Keep it short; if there is no response, it is treated as "unknown" and considered to have free slots (fail-safe;\n'
        'when it cannot be checked, it falls back to the old behavior without checking).'
    ),
    'llm.round_robin_busy_poll_interval_seconds': (
        'Seconds to wait before checking again with GET /slots when none of the round_robin/priority_failover candidates (endpoints usable in the current time range) had free slots.'
    ),
    'graph.implementation': (
        'Switches the implementation of the ReAct loop.\n'
        '"handwritten" : a handwritten StateGraph (the agent/tools nodes are wired by hand; easy to follow the behavior)\n'
        '"prebuilt"    : LangGraph\'s standard create_react_agent (minimal implementation, but the internals are hidden)'
    ),
    'graph.recursion_limit': (
        "Maximum number of iterations of the main ReAct loop (agent -> tools transitions). Passed as is to LangGraph's recursion_limit. When exceeded, app.py catches GraphRecursionError and shows a cutoff message to the user.\n"
        'The unit is node transitions (one step each for agent/tools), which is counted differently from [subagent] max_iterations (agent <-> tools round trips).'
    ),
    'graph.connection_error_max_retries': (
        'When a communication error with the LLM server (connection failure, 5xx, httpx errors) is detected, the most recently used endpoint is put on a temporary cooldown (only with main_routing_strategy=priority_failover does it switch to the next endpoint; round_robin/random switch on every call anyway, so effectively the next one is used), the LLM graph is rebuilt, and the same iteration is retried automatically up to this many times. 0 means no retry: the user is notified of the communication error and the turn is aborted (the old behavior). The sub-agent (dispatch_agent) side uses background_llm_timeout_max_retries in the [subagent] section.'
    ),
    'graph.max_parallel': (
        "How many tool calls of the main agent (via ImageAwareToolNode) may run in parallel at the same time per session. LangGraph's ToolNode runs multiple tool_calls in the same AIMessage fully in parallel with asyncio.gather(), so when the model calls several tools in one turn, the call order and the order in which responses are processed can diverge, and simultaneous access to shared resources (llama-server, DBs, etc.) can happen (the main agent version of the same reasoning as [subagent] max_parallel).\n"
        'Each session (thread_id) has its own asyncio.Semaphore (see _TOOL_CALL_SEMAPHORES in src/tools.py), so this value is the upper limit of "the slots one session can use at the same time"; it does not prevent several sessions from using these slots at the same time.\n'
        '1 or more: guard the number of concurrent runs to that value with asyncio.Semaphore(N) (default 1 = fully serialized; the call order is also kept to some extent by the FIFO queue of asyncio.Semaphore).\n'
        '0 or less: disable the guard and allow parallel calls as they are.\n'
        'Note: even if this value is larger than [llm].max_concurrent_requests, tools that use the LLM (analyze_image, etc.) just wait for a free LLM slot and do not error. Tools that do not use the LLM run concurrently up to this value (for details see the comment of [llm].max_concurrent_requests).\n'
        'Note: raising this value only parallelizes the execution of tool calls; it is unrelated to the routing of endpoints by main_url/main_routing_strategy.\n'
        'The model endpoint of the main agent is chosen only once, according to main_routing_strategy, when the graph is built (when a conversation starts or resumes), and the same endpoint is used throughout that session (build_model() is not called on every generation;\n'
        'see the build_model() docstring in src/llm/chat_model.py). In other words, making max_parallel 2 or more does not distribute the load across several main_url endpoints.'
    ),
    'graph.token_guard_enabled': (
        'When the total_tokens of the main agent\'s most recent LLM response reaches the threshold, the text of handoff_prompt_path is inserted into the input of the next model call, making the model output "a status report to the user" and "a handoff prompt for resuming in a new chat" and finish safely (see src/main_token_guard.py). The main agent version of [subagent].token_guard_* on the sub-agent side. The LLM itself cannot know its own token usage, so the code decides. With track_token_usage=false, usage_metadata cannot be obtained and it effectively never fires.'
    ),
    'graph.token_guard_soft_threshold': (
        "With low-parameter models, processing cannot continue once the number of tokens per request exceeds a certain level. Set this to that effective limit (the context length determined by llama-server's --ctx-size, e.g. 256000) minus a margin for the growth of one round trip of tool results. The guideline in the README's design principles (128,000 or fewer per request) is the value to keep in normal processing; this threshold is a safety device for when it keeps growing beyond that, so it may be larger than the guideline."
    ),
    'graph.handoff_prompt_path': (
        'Markdown file with the text inserted above (the procedure for handing off to a new chat).'
    ),
    'subagent.max_iterations': (
        'Maximum number of iterations of the ReAct loop that the dispatch_agent tool runs internally (the number of agent -> tools transitions).\n'
        'When exceeded, it is cut off, and the tool results collected up to that point are summarized and returned.\n'
        '\n'
        'This value is referenced from system_prompt.md as ${subagent_max_iterations}, and is also used as "the guideline for the number of files passed to one dispatch_agent (batch size)".\n'
        'That is a different use from the original meaning (number of iterations), but to avoid hard-coding numbers with no basis in the prompt, the source is kept in this one place.\n'
        'Supplement: sub-agents issue multiple tool calls in parallel in the same turn, so in practice the number of iterations levels off at around 2, and this value is almost never the constraint on iterations. The practical upper limit is the token_guard_* settings below. So if you want to increase or decrease the batch size, changing this value alone is not enough; also look at token_guard_*.'
    ),
    'subagent.max_parallel': (
        'How many real LLM calls of the dispatch_agent tool are allowed at the same time per session. When the model calls dispatch_agent several times in one turn, ToolNode runs them in parallel by design. When the backend is a single llama-server instance and more parallel requests are sent than the number of slots set with --parallel, the ToolMessages corresponding to the tool_calls of the AIMessage can go missing, corrupting the checkpoint, and LangGraph can raise ValueError on the next model call; this guard was introduced for that (_DISPATCH_AGENT_SEMAPHORES in src/tools.py).\n'
        'Each session (thread_id) has its own asyncio.Semaphore, so this value is the upper limit of "the slots one session can use at the same time"; it does not prevent several sessions from using these slots at the same time.\n'
        '1 or more: guard the number of concurrent runs to that value with asyncio.Semaphore(N).\n'
        'It is safest to match the --parallel value of the llama-server in operation (default 1 = fully serialized).\n'
        '0 or less: disable the guard and allow parallel calls of dispatch_agent as they are (for testing with several llama-server instances, etc.).\n'
        'Note: each group (= sub-agent) of dispatch_agent_batch also uses the same slots. batch starts all groups at once, but only up to this many run at the same time, and the rest wait until a slot frees up (e.g. with this value=2 and 5 groups, at most 2 always run, and each time one finishes the next one starts). [graph].max_parallel uses only one slot for the whole batch call, so it has nothing to do with the number of groups running at the same time.\n'
        'Note: this slot is occupied from the start to the end of the sub-agent (the whole repetition of LLM calls and tool execution). If this value is larger than [llm].max_concurrent_requests, sub-agents start up to this number. But only max_concurrent_requests of them can generate with the LLM at the same time, and the rest wait just before calling the LLM (no error; the waiting time is not counted toward timeouts). For details see the comment of [llm].max_concurrent_requests.\n'
        'Note: unlike [graph].max_parallel, build_model(role="sub") is called separately for each dispatch_agent started in parallel, so routing by sub_routing_strategy actually works. But only when you set several sub_url entries of your own (sub_endpoints_inherit_main=false). When sub_url is not set (default, sub_endpoints_inherit_main=true), all dispatch_agent calls started in parallel inherit the main agent\'s most recently chosen endpoint as is, so the load is not distributed and all of them concentrate on the same endpoint (see inherit_from_role of _select_endpoint() in src/llm/routing.py).'
    ),
    'subagent.batch_max_groups': (
        'Upper limit on the number of groups (= sub-agents) that dispatch_agent_batch may start in one call. If the number of files ÷ group_size (rounded up) exceeds this value, an error is returned without starting any job (a safety valve that prevents hundreds of jobs from starting without confirmation because a wrong pattern matched a whole large folder).\n'
        'It is not the number running at the same time (that is decided by max_parallel above). 0 or less means unlimited.'
    ),
    'subagent.batch_stop_after_unreachable_groups': (
        'During a dispatch_agent_batch run, when the number of groups cut off because LLM timeouts / communication errors continued even after background_llm_timeout_max_retries retries reaches this number, groups not started yet are not started and are returned as "not started" (prevents every group from waiting "timeout × number of retries" and being cut off while the LLM server stays down). Groups already running keep running to the end. 0 or less disables it (all groups are always started).'
    ),
    'subagent.token_guard_enabled': (
        'Monitors usage_metadata.total_tokens of each LLM response within dispatch_agent, and when the threshold is exceeded applies two-stage control: "warning (soft) -> cutoff (hard)".\n'
        'The LLM itself cannot know its own token usage, so the code (src/subagent.py) decides. With track_token_usage=false, usage_metadata cannot be obtained and it effectively never fires.'
    ),
    'subagent.token_guard_soft_threshold': (
        'When the total_tokens of the most recent response reaches this value or more, the tool_calls of that iteration run as usual, and then a warning message telling it to "wrap up and answer" is injected once before the next model call. Keep it at a level lower than [llm].max_tokens (the absolute limit on the model side) and [context_compaction].token_threshold (the compaction threshold of the whole main graph).'
    ),
    'subagent.token_guard_hard_threshold': (
        'When a response with total_tokens at or above this value arrives, it is judged on the spot regardless of whether the soft warning has fired: its tool_calls are not run, and model.ainvoke() is not called any more; the run is cut off (after saving to a scratch note as when force-stopped with the stop button, it returns in the same summary format as when max_iterations is reached). It fires immediately even when a rapid token explosion jumps over the soft warning straight to this value. Give it some margin over token_guard_soft_threshold. Keeping it below [context_compaction].token_threshold (the compaction threshold of the whole main graph) is intended to make the sub-agent converge on its own before the main graph reaches its compaction threshold.'
    ),
    'subagent.token_guard_soft_warning_text': (
        'The text of the warning message injected once when the soft threshold is reached. Make it tell the LLM "first write unrecorded facts with write_thread_note, and then keep the next response concise" (with just "wrap up and answer in the next response", it copies the investigation results wholesale into the body of the final answer without writing anything with write_thread_note, and the purpose of writing them out = avoiding bloat in the delegator\'s conversation history is not achieved). Keep allowing tool calls themselves (forbidding write_thread_note calls would defeat the purpose).\n'
        "Due to configparser's specification, to include line breaks, indent the following lines with spaces and write them as continuation lines."
    ),
    'subagent.empty_response_max_retries': (
        'If an LLM response within dispatch_agent has neither tool_calls nor a body (an abnormal response on the LLM server side), treating it as a normal end and returning an empty string would swallow all investigation results so far, and nothing would reach the delegator.\n'
        'It retries up to this many times, and only if the response is still empty, summarizes the tool results collected so far and cuts off.'
    ),
    'subagent.background_job_retention_seconds': (
        'Seconds a dispatch_agent job may stay in the registry after it finishes without ever being fetched with check_dispatch_agent_job. Jobs over this are discarded on the next dispatch_agent call (the same scheme as [scripts].background_job_retention_seconds).'
    ),
    'subagent.background_min_poll_interval_seconds': (
        'Minimum interval in seconds, right after check_dispatch_agent_job returns the "running" status, before the next check_dispatch_agent_job call for the same job is allowed.\n'
        'One iteration of a sub-agent involves an LLM call and changes state less per unit time than script runs in [scripts], so the default is the same as on the [scripts] side.\n'
        '0 or less disables the enforcement.'
    ),
    'subagent.background_min_poll_message': (
        'The message returned to the LLM instead of a refusal when check_dispatch_agent_job is called before the minimum interval above. If empty, the built-in default text is used. The placeholders are the same as [scripts].background_min_poll_message ({wait_remaining}/{job_id}/{min_interval}).'
    ),
    'subagent.background_inline_wait_max_seconds': (
        'The dispatch_agent tool call waits for the job to finish on the code side, without going through the LLM, for up to this many seconds (in the Chainlit UI the turn stays blocked and the "running" display continues). Only if it is still running after this many seconds is the job_id returned and control given back to the LLM (fallback; after that it is checked with check_dispatch_agent_job). The job itself is not canceled when this limit is reached and keeps running in the background.\n'
        "0 or less waits indefinitely, and this safety-limit fallback itself no longer happens (no job_id is ever returned, and check_dispatch_agent_job / stop_dispatch_agent_job are effectively no longer used). To cancel, use Chainlit's stop button (the internal job is also reliably stopped)."
    ),
    'subagent.background_progress_push_interval_seconds': (
        'While waiting as above, the interval (seconds) at which the elapsed seconds, number of iterations and the end of the progress notes are sent directly to the chat for humans. It only sends cl.Message and involves no LLM call, so it consumes no tokens.\n'
        'The first display appears after this many seconds. After that, when the current activity ([Thinking], [Tool: run script], [Waiting for a free LLM slot], etc.) changes, it is rewritten without waiting for this interval (but at least 1 second after the previous one).'
    ),
    'subagent.background_llm_timeout_max_retries': (
        'When an LLM call during a dispatch_agent run reaches [llm].request_timeout_seconds / stream_chunk_timeout_seconds, or a communication error (connection failure, 5xx, httpx errors) is detected, the most recently used endpoint is put on a temporary cooldown (only with sub_routing_strategy=priority_failover does it switch to the next endpoint), the model is rebuilt, and the same iteration is retried up to this many times (the same scheme as the rebuild-and-retry for ThinkingLoopDetected). The job itself keeps running as a background task, so retrying is more reasonable than giving up the progress of hundreds of items over one timeout. The main agent side uses [graph].connection_error_max_retries.'
    ),
    'subagent.agent_type_run_script_allowlist': (
        'A whitelist that restricts the skills/scripts callable with run_script per agent_type of dispatch_agent sub-agents (agent_types that are not registered are unrestricted).\n'
        'What you register here is automatically inserted, narrowed down per agent_type, into the {{run_script_allowlist}} placeholder of agents/*.md (the same mechanism as {{skills}}). For agent_types with entries, the skill list of {{skills}} is also narrowed down to the skills registered here (if you do not want that, write {{all_skills}} instead of {{skills}} in agents/*.md to list all skills).\n'
        'Specify it as a Python/JSON-style list [[agent_type, target], ...] (trailing comma allowed).\n'
        'If empty, the 19 default entries below are used. A target is one of these two kinds:\n'
        '- "skill name"                         : allow all scripts under that skill\n'
        '- ["skill name","script file name"]   : allow only that script\n'
        'For skills like pdf-tools that mix read-only scripts (read_pdf.py / render_pdf_pages.py) and write scripts (create_pdf.py) under the same skill, allow them individually with the latter form, not per skill name.\n'
        'A value saved on the settings dashboard (the per-instance config_overrides.json) does not replace the defaults; only entries not in the defaults are added (default entries cannot be removed, so that new skills added to the defaults by an update are not wiped out by a previously saved value).'
    ),
    'context_trim.enabled': (
        'Settings for the main agent (src/graph.py). Sub-agents (dispatch_agent, run_subagent in src/subagent.py) use the values in the [context_trim.subagent] section below where items are given, and items not given inherit the values of this section (by default no items are given = full inheritance, so unless you edit [context_trim.subagent], the same values apply to main and sub as before).\n'
        'A countermeasure for long ReAct loops (tasks that repeat tool calls such as reading files), where tool results with no size limit (e.g. the full Markdown of OCR results) keep piling up as ToolMessages in the conversation history and prompt prefill on llama.cpp becomes extremely slow. The content of ToolMessages other than the most recent ones is truncated, narrowing down only the input to the LLM (the conversation history itself is not rewritten; see src/context_trim.py).\n'
        'Not recommended, because trimming temporarily breaks the prompt cache.'
    ),
    'context_trim.trigger_total_tokens': (
        "The threshold that triggers trimming (equivalent to trigger.value of clear_tool_uses_20250919 in the Claude API's context editing). While the total_tokens of LLM calls is below this value, trimming does not kick in (short conversations are not truncated). Once the threshold has been reached, trimming continues for the rest of that conversation (repeatedly lifting and re-applying it every time trimming brings the input below the threshold would miss llama-server's prefix cache each time and redo the full prefill). Trimming is lifted when context compaction ([context_compaction]) runs. 0 or less makes it always kick in (the same as the old behavior before this threshold existed). Note that when total_tokens cannot be obtained, e.g. with [llm].track_token_usage=false, the threshold is considered not reached and it does not kick in."
    ),
    'context_trim.keep_recent_tool_iterations': (
        'Number of most recent iterations kept in full (find_iteration_cut_index in src/context_trim.py).\n'
        '"One iteration" here is one round of the ReAct loop = one LLM call (one AIMessage and its corresponding ToolMessages); one agent -> tools transition of the main agent or one iteration of a sub-agent is the unit as is. Only ToolMessages older than this are truncated.\n'
        'It is not counted in user messages: image follow-ups of analyze_image and various nudges (loop guards, the token_guard soft warning, hallucination retries) are stacked in the history as HumanMessages, so it would drift from the number of user messages. The more injections, the more the kept range would widen unintentionally, and trimming would stop working.\n'
        'It is not a simple message count either: that avoids splitting the ToolMessages corresponding to the multiple tool_calls issued in parallel by one AIMessage, keeping some and truncating the rest (one iteration always goes entirely to the same side).'
    ),
    'context_trim.truncated_max_chars': (
        'How many leading characters of the content of truncated ToolMessages / AIMessages to keep (the rest is replaced with a marker text).'
    ),
    'context_trim.duplicate_guard_tool_max_chars': (
        'Truncation length applied only to ToolMessages of Read/Glob/Grep/json_query/analyze_image (the tools covered by [file_tools_duplicate_guard], which limit the number of calls with the same arguments) (used instead of truncated_max_chars). When the limit is reached, these tools say "refer to the previous result in the conversation history", but if the previous result was truncated to truncated_max_chars (kept small to shorten prefill), the model would be told to refer to parts it never actually received, and the guidance would not work. So they get a separate, larger value.'
    ),
    'context_trim.trim_ai_messages': (
        "Whether to truncate not only ToolMessages but also AIMessages (the model's own reasoning text and tool_call arguments). With usages like copying file contents into the code argument of execute_python_code, narrowing only the ToolMessage side does not stop the input per request from growing and sticking to the context limit, so it is enabled by default. The id/name and number of tool_calls are not changed, so the correspondence with ToolMessages is not broken."
    ),
    'context_trim.keep_recent_ai_iterations': (
        'Number of most recent iterations kept in full (the same method as keep_recent_tool_iterations;\n'
        'it can be set independently of the tool side). Only AIMessages older than this are truncated.'
    ),
    'context_compaction.enabled': (
        "Equivalent to Claude Code's compact. When either of the following two conditions is met, the LLM itself summarizes the old conversation history and the persistent history (the messages in the checkpointer) itself is compacted (see src/context_compaction.py):\n"
        "1. The main agent's cumulative token count (token_usage_cumulative_main, reset to 0 each time compaction fires) exceeds token_threshold.\n"
        '2. The total_tokens of the most recent LLM call exceeds single_request_token_threshold.\n'
        'With only the cumulative condition, the payload reduction by [context_trim] keeps the token count of individual recent requests below the threshold, so compaction never fires while only the persistent history grows, and when it finally fires, the summarizing LLM call itself can exceed the context length. Conversely, with only the single-request condition, it does not fire however long the conversation gets unless a huge tool result or file content is stacked in one turn. So both are checked.\n'
        '[context_trim] follows the policy "shrink only the current input, never rewrite the persistent history", whereas this shrinks the persistent history itself, so it is a permanent countermeasure against prefill delay and token volume, which matter more the longer the conversation gets.\n'
        'Like [context_trim], sub-agents (dispatch_agent) use the values in the [context_compaction.subagent] section below where items are given, and items not given inherit the values of this section (full inheritance by default).\n'
        'But the starting point of "the cumulative token count" differs between main and sub (main = cumulative over the whole thread, sub = cumulative only within one dispatch_agent call; both are reset to 0 each time it fires).'
    ),
    'context_compaction.token_threshold': (
        'Compact when the main agent\'s cumulative total_tokens exceeds this value (it is reset to 0 each time compaction fires, so it means "the cumulative limit per compaction cycle"; note that it is not a per-request limit).'
    ),
    'context_compaction.single_request_token_threshold': (
        'Compact when the total_tokens of the most recent LLM call exceeds this value.\n'
        'With low-parameter models, each request must stay below the context window, so set it lower than that, with a margin for one round trip of tool results.'
    ),
    'context_compaction.keep_recent_iterations': (
        'Number of most recent iterations kept whole when compacting (the same unit as keep_recent_tool_iterations of [context_trim] = one round of the ReAct loop = one LLM call).\n'
        'A safe cut point is searched for structurally (matching issued tool_call ids with returned ToolMessage ids), and the older side is cut off there as the target of summarization.\n'
        'Since compaction is a permanent operation that replaces the persistent history with a summary, an additional internal limit, "always keep everything from the last HumanMessage onward verbatim", is applied on top of this iteration count (_find_compaction_cut_index in src/context_compaction.py). But when applying that limit as is would leave nothing to compact (long tasks that repeat LLM calls dozens of times within one turn), it is not applied, since the chance to compact would be lost altogether.'
    ),
    'context_compaction.min_messages_to_compact': (
        'If the conversation has fewer messages than this, it is not compacted even if a threshold is exceeded (a safety valve against running summaries needlessly on short conversations).'
    ),
    'context_compaction.compaction_prompt_path': (
        'Path of the prompt text (Markdown) that instructs the summary.'
    ),
    'context_compaction.summary_source_max_chars': (
        'Number of characters to which old ToolMessages being summarized are truncated before being passed to the summarizing LLM (so that the prefill of the summary prompt itself does not get slow).\n'
        'Separate from [context_trim].truncated_max_chars. Summarizing is a permanent operation that replaces the persistent history, so using the same small value as [context_trim] would lose information across the summarized tool results and could make the summary thin (e.g. in tasks processing many files, only a list of file names remains).'
    ),
    'context_compaction.skill_reattach_max_chars_per_skill': (
        'read_skill results (skill bodies) that fall within the summarized part are not summarized but reattached verbatim after the summary (so that the summarizing LLM cannot water down the procedures; _render_reattached_skills in src/context_compaction.py). Skills are packed starting from the most recently read, anything over the per-skill / total character limits is truncated, and older skills that do not fit in the total are only listed by name, prompting a reread with read_skill. Sub-agents share this value (it cannot be overridden in [context_compaction.subagent]).'
    ),
    'context_compaction.skill_reattach_total_max_chars': (
        'Upper limit on the total characters of the skill bodies reattached as above. Skills are packed starting from the most recently read, and older skills that do not fit are only listed by name, prompting a reread with read_skill.\n'
        'Make it at least skill_reattach_max_chars_per_skill. Sub-agents share this value (it cannot be overridden in [context_compaction.subagent]).'
    ),
    'context_compaction.pre_note_threshold': (
        'The threshold (based on the total_tokens of the most recent LLM call) at which, before compaction (summarization) fires, a warning message is injected urging the model to write out important facts (values, counts, locations, etc.) not yet written with write_thread_note. Since the LLM itself does the summarizing, concrete facts in the old conversation that fall outside the summary can be watered down and lost. write_thread_note appends to a file and is not affected by summarizing, so the aim is to save them before they are lost (maybe_append_precompact_note_nudge in src/context_compaction.py). Make it sufficiently smaller than single_request_token_threshold (to leave room to call write_thread_note in "the next response" after the injection, before compaction actually fires). 0 or less disables this feature. It is different from [graph].token_guard_soft_threshold (a complete cutoff that ends the whole conversation and urges handing off to a new chat); this is a light hook that only warns without stopping the conversation (if both fire at the same time, the handoff takes precedence and this is not inserted, because "do not call tools" and "call write_thread_note" contradict each other).\n'
        'Note: injecting this message unconditionally every turn while over this threshold would effectively create an infinite loop in which the main agent keeps rewriting notes with the same content.\n'
        'So the implementation adds a check "do not reinject if write_thread_note has already been called within the last keep_recent_iterations iterations" (_write_thread_note_called_recently), reusing keep_recent_iterations (so as not to add another setting). Note that changing keep_recent_iterations also changes the range of this re-nudge suppression.'
    ),
    'context_compaction.pre_note_warning_text': (
        "The text of the warning message injected when the threshold above is reached. Make it urge writing out with write_thread_note. Due to configparser's specification, to include line breaks, indent the following lines with spaces and write them as continuation lines."
    ),
    'context_compaction.require_note_max_skips': (
        'Adds a further condition to firing compaction: "write_thread_note has been called within the last keep_recent_iterations iterations". While it has not been called, even if token_threshold/single_request_token_threshold is met, _run_context_compaction in app.py forces writing out on the spot with force_write_thread_note (src/context_compaction.py) (it calls the model once with all tools other than write_thread_note made unusable; achieved by passing tool_choice="required" narrowed to that one tool.\n'
        'With only the warning by pre_note_threshold, if the LLM keeps ignoring it, compaction could happen with nothing recorded at all, so instead of waiting it is forced).\n'
        'This value is a safety valve for when force_write_thread_note itself keeps failing (LLM call errors, empty tool_calls, state update failures, etc.); when the number of failures reaches this value, compaction is forced even without a record (prevents sticking to the context limit while being ignored). 0 waits indefinitely (no compaction until forcing write_thread_note succeeds; note that it is then left to the cutoff by the token_guard soft/hard thresholds).'
    ),
    'token_ratio.enabled': (
        'A setting used only by the settings dashboard (admin tool). The app itself (src/) does not read this section.\n'
        'Holds each token-control threshold as a ratio to [graph].token_guard_soft_threshold (the base value), and when the base value is changed on the dashboard, "base value x ratio" (rounded to an integer) is filled into each threshold automatically. The filled values are saved to config_overrides.json through "Review changes" -> "Save" like any other change, so what is used at startup is the value saved for each threshold (it is not recalculated from the ratio every time).\n'
        'Each key name below is the "section.key" to fill in. A key whose value is left blank is not linked (changing the base value does not change that threshold).\n'
        'While enabled below is true, changing a ratio also fills that threshold with "current base value x new ratio".\n'
        'Whether to fill in automatically when the base value or a ratio is changed. false keeps the ratios but stops only the linking (the "Recalculate from ratios" button of the "Token control" preset works even when false).'
    ),
    'token_ratio.subagent.token_guard_soft_threshold': (
        'Ratio for [subagent].token_guard_soft_threshold. Keep it below 1, since sub-agents should wrap up before the main agent.'
    ),
    'token_ratio.subagent.token_guard_hard_threshold': (
        'Ratio for [subagent].token_guard_hard_threshold. Make it larger than the ratio of subagent.token_guard_soft_threshold.'
    ),
    'token_ratio.context_trim.trigger_total_tokens': (
        'Ratio for [context_trim].trigger_total_tokens.'
    ),
    'token_ratio.context_compaction.single_request_token_threshold': (
        "Ratio for [context_compaction].single_request_token_threshold. Keep it below 1 so that compaction kicks in before the main agent's handoff (the base value)."
    ),
    'token_ratio.context_compaction.pre_note_threshold': (
        'Ratio for [context_compaction].pre_note_threshold. Make it sufficiently smaller than the ratio of context_compaction.single_request_token_threshold (to leave room to call write_thread_note in "the next response" after the injection).'
    ),
    'scripts.timeout': (
        'Execution settings shared by the run_script tool and the execute_python_code tool.\n'
        'Execution timeout in seconds (shared by run_script / execute_python_code).'
    ),
    'scripts.python': (
        'Python executable that starts .py scripts (shared by run_script / execute_python_code).\n'
        'If empty, the Python running Locohane itself (PYTHON_DIR in python_env.bat) is used.'
    ),
    'scripts.code_execution_enabled': (
        'Enables/disables the execute_python_code tool (runs Python code generated by the LLM on the spot).\n'
        'If false, the tool just returns an error even when called, and no code is executed.'
    ),
    'scripts.background_max_runtime_seconds': (
        'From here on, settings only for run_script_background (the background version).\n'
        'Upper limit in seconds before a background job is forcibly terminated (a separate axis from timeout;\n'
        'a limit assuming longer runs than the synchronous run_script).'
    ),
    'scripts.background_job_retention_seconds': (
        'Seconds a job may remain after it ends (completed, failed, timed out, etc.) without ever being fetched with check_script_job. Jobs over this are discarded on the next run_script_background call.'
    ),
    'scripts.background_min_poll_interval_seconds': (
        'Minimum interval in seconds, right after check_script_job returns the "running" status, before the next check_script_job call for the same job is allowed. Even when SKILL.md and the tool docstrings say "do not call it again every few seconds", there were cases where the local LLM ignored that and kept rechecking every few seconds, so the server side enforces it (calls before the interval only return background_min_poll_message below, without stdout/stderr). 0 or less disables the enforcement.'
    ),
    'scripts.background_min_poll_message': (
        'The message returned to the LLM instead of a refusal when check_script_job is called before the minimum interval above. If empty, the built-in default text ("Checked too soon. Wait about N more seconds, then ...") is used. The following 3 placeholders can be embedded with Python\'s str.format() notation (writing any other name causes an error at server startup).\n'
        '{wait_remaining} : how many more seconds to wait before calling again (integer, rounded up)\n'
        '{job_id}         : the target job ID (repr form; writing {job_id!r} embeds it with quotes, so it can be pasted as is into check_script_job(job_id=...))\n'
        '{min_interval}   : the value of background_min_poll_interval_seconds itself\n'
        'Example:\n'
        'background_min_poll_message = The polling interval is too short. Run check_script_job(job_id={job_id!r}) again in about {wait_remaining} seconds (minimum interval: {min_interval} seconds).'
    ),
    'scripts.background_inline_wait_max_seconds': (
        'The run_script_background/execute_python_code_background tool calls wait for the job to finish on the code side, without going through the LLM, for up to this many seconds (in the Chainlit UI the turn stays blocked and the "running" display continues). Only if it is still running after this many seconds is the job_id returned and control given back to the LLM (fallback; after that it is checked with check_script_job). The job itself is not canceled when this limit is reached and keeps running in the background.\n'
        "0 or less waits indefinitely, and this safety-limit fallback itself no longer happens (no job_id is ever returned, and check_script_job/stop_script_job are effectively no longer used). To cancel, use Chainlit's stop button (the internal job is also reliably stopped)."
    ),
    'scripts.background_progress_push_interval_seconds': (
        'While waiting as above, the interval (seconds) at which the elapsed seconds and the end of stdout/stderr are sent directly to the chat for humans. It only sends cl.Message and involves no LLM call, so it consumes no tokens.'
    ),
    'scripts.background_job_output_tail_chars': (
        'Maximum number of characters of stdout/stderr/progress notes when the progress display above, and check_script_job/stop_script_job/read_thread_note, show only the end ("running", "at the time of forced termination", etc.). Showing everything would squeeze the context, so it is truncated.\n'
        'The progress display of dispatch_agent (under [subagent]) shares the same value.'
    ),
    'file_tools_duplicate_guard.enabled': (
        'Settings of the guard that prevents calling the Read/Glob/Grep/json_query/read_skill/read_skill_file/get_tool_source tools repeatedly with the same arguments. These are read-only, which guarantees that rerunning them with the same arguments does not change the result (a countermeasure for cases where small local models sporadically repeat the same call throughout the conversation without progress. For read_skill/read_skill_file it also counters thrashing, where the same large SKILL.md etc. is reread again and again after context compaction removed the earlier result from the message history.\n'
        'See _check_file_tools_duplicate / _record_and_check_duplicate in src/tools.py).'
    ),
    'file_tools_duplicate_guard.max_calls': (
        'How many calls with the same signature (tool name + arguments) are allowed (calls after reaching this number are refused with an error). The default is 1 (= only the first call is allowed; the second and later are refused).'
    ),
    'file_tools_duplicate_guard.carry_over_to_main': (
        "Whether the call history within sub-agents (dispatch_agent) carries over to the main agent's checks.\n"
        'true          : the main agent and sub-agents share the set of calls.\n'
        'For example, if the main agent later tries to read a file a sub-agent has read, with the same arguments, it is refused as a duplicate.\n'
        "false         : the call histories of the main agent and sub-agents are managed separately. Calls within sub-agents no longer affect the main agent's checks (and conversely the main agent's call history does not affect the sub-agents' checks)."
    ),
    'main_agent_tool_guard.mode': (
        'A general-purpose guard that limits how many times the main agent itself can call "registered tools" directly. Both built-in tool names (e.g. Glob, analyze_image) and skill scripts under run_script (e.g. pdf-tools/render_pdf_pages.py) can be registered in the same list, each with its own limit (max_calls) (the old Glob-only guard main_agent_glob_guard has been merged into this guard; the default allow_entries include ["Glob", 1] to keep the same behavior as before).\n'
        '[plan].plan_approval_exempt_scripts only exempts scripts from plan approval (Plan Mode); it does not stop the main agent from calling exempted scripts directly and repeatedly.\n'
        'So the aim is to prevent situations such as the main agent turning a scanned PDF into images with render_pdf_pages.py and then hammering analyze_image on dozens of them itself, without delegating, until it hits the token limit. Calls within sub-agents (dispatch_agent) are not covered (investigating is their job). See _guard_main_agent_tool_limit in src/tools.py.\n'
        'false             : guard disabled (the main agent can call all tools without limits).\n'
        'tools_skills_only : dynamic tools via MCP servers (the [mcp] section; in the form mcp__<server name>__<tool name>) are always allowed, and built-in tools and run_script skills are limited by allow_entries below as before. MCP server tool names are only known at run time and cannot be registered in allow_entries in advance, so this mode is convenient when using MCP servers.\n'
        'all               : limit all of built-in tools, run_script skills and MCP dynamic tools with allow_entries (equivalent to the old enabled=true).'
    ),
    'main_agent_tool_guard.visibility_mode': (
        'Visibility mode. A setting independent of mode (on/off/scope of the call limit itself); this value alone decides how the lists are shown, whatever the value of mode. It covers the skill list and the notice of built-in tool names that cannot be called; MCP dynamic tools are not what this description has in mind (but with mode=all the notice of names that cannot be called covers get_all_tools() = built-in + all MCP, so MCP dynamic tool names can be mixed in;\n'
        'with mode=tools_skills_only MCP is always allowed, so they are not mixed in).\n'
        'all           : show all skill names, tool names and descriptions in the list, but append "(cannot be run directly; delegate running it to dispatch_agent.)" to the end of the description of those that cannot be run directly. For built-in tools, only the list of names that cannot be called is presented as text (they are not bound).\n'
        'With mode=false nothing is uncallable, so no notes are added.\n'
        'strict (default): remove skills and tools that cannot be called from the lists entirely (the old behavior; prevents wasted round trips of "visible but refused when called").\n'
        'With mode=false nothing is uncallable, so everything is shown.\n'
        'hint          : show all skill names in the list, but replace the description of those that cannot be run directly with "cannot be run directly. Delegate checking details of and running this skill to dispatch_agent". For built-in tools, only the list of names that cannot be called is presented as text (they are not bound).\n'
        'With mode=false nothing is uncallable, so everything is shown (no descriptions are replaced either).'
    ),
    'main_agent_tool_guard.allow_entries': (
        '- dispatch_agent / dispatch_agent_batch / check_dispatch_agent_job / stop_dispatch_agent_job (the delegation mechanism itself; delegating to several folders in one turn is normal usage)\n'
        '- create_plan / approve_plan / update_task_progress / get_plan_status / lock_plan_mode (plans normally have several steps, and update_task_progress is called once per step)\n'
        '- AskUserQuestion / ask_user_choice (may be called several times to confirm several items)\n'
        '- create_memory / update_memory / delete_memory / read_memory / search_memory / list_memories (may handle several memories in one turn; contents are small and the token risk is low)\n'
        '- read_skill / read_skill_file / get_tool_source (reading several skills in one turn is normal)\n'
        '- check_script_job / stop_script_job (job management)\n'
        '- check_work_dir_status / search_path_memory / provide_download / help / write_thread_note / list_thread_notes / read_thread_note (lightweight tools with low token risk)\n'
        '- list_document_collections / search_documents / search_document_paths (vector DB search; the whole result is truncated to [vector_db].max_result_chars, so the token risk is low)\n'
        '- analyze_image (with show_image merged into it, the main agent can also call it directly.\n'
        'But 1-2 images is the guideline. Hammering many pages can hit the token limit, so the "Handling images" section of system_prompt.md urges delegating large numbers to worker/explore)\n'
        'run_script / run_script_background are not registered as bare tool names (registering them would limit them all together regardless of which skill script is run, and the granularity of [skill name, script file name] below would lose its meaning).\n'
        'So when you add a new skill script, the main agent cannot run it unless you add it individually to allow_entries below (it can be run via sub-agents).\n'
        'A value saved on the settings dashboard (the per-instance config_overrides.json) does not replace the defaults; only targets not in the defaults are added (default targets cannot be removed and their max_calls cannot be changed, so that new skills added to the defaults by an update are not wiped out by a previously saved value).'
    ),
    'thinking_loop_guard.enabled': (
        'Settings of the feature that, when a repetition loop (a runaway in which the same sentence or code block repeats almost verbatim) is detected while streaming an LLM response (thinking/body), cuts off generation, injects a warning message and retries. [llm].dry_multiplier (the server-side DRY sampler) suppresses this greatly, but this is an app-side (client-side) safety net for the residual risk that does not disappear completely.'
    ),
    'thinking_loop_guard.target': (
        'What loop detection monitors.\n'
        'all          : monitor both thinking and the body (as before).\n'
        'content_only : monitor only the body. Thinking loops are left to the cutoff by [llm].reasoning_budget (the thinking budget). The thinking budget has no false positives, and a model cut off by it goes straight on to write the answer (tool calls), so for thinking loops of the "decided but cannot move to execution" kind it works more straightforwardly than aborting and retrying. On the other hand, the rest of the thinking may leak into the body when cut off (see the comment of [llm].reasoning_budget).\n'
        'thinking_only: monitor only thinking. Body loops are not detected (loops that repeat exactly the same response are detected separately by [tool_loop_guard]).\n'
        'With content_only, if [llm].reasoning_budget is empty or -1 (unlimited), nothing stops thinking loops, so a warning is given at startup.'
    ),
    'thinking_loop_guard.window_chars': (
        'Window size in characters of the most recent text being checked.'
    ),
    'thinking_loop_guard.check_interval_chars': (
        'Recheck every time it grows by this many bytes.'
    ),
    'thinking_loop_guard.confirm_count': (
        'How many consecutive times the condition above must hold before it is confirmed as a loop (prevents false positives).'
    ),
    'thinking_loop_guard.max_history_chars': (
        'Repetition detection that does not depend on the period. It measures dynamically, with difflib.SequenceMatcher.find_longest_match(), how long a substring matching the most recent window_chars characters exists in the earlier history (limited to at most max_history_chars characters).\n'
        'Upper limit in characters of the past history compared. Larger values detect repetitions with longer periods but cost more computation.'
    ),
    'thinking_loop_guard.match_ratio_threshold': (
        'When match_ratio (the longest match length with the most recent window / window_chars) exceeds this value, it is considered a candidate for "repetitive".'
    ),
    'thinking_loop_guard.max_retries': (
        'Maximum number of retries, injecting a warning message, after a loop is detected (shared by the main graph and sub-agents).\n'
        'Consumed independently of empty_response_max_retries below (see ainvoke_ensuring_final_text in src/graph.py). In tasks with dozens to hundreds of LLM calls in one turn, like processing many files, independent failures can occur early and late, so there is some margin. Cases where thinking hits the token limit and is cut off in complex, information-heavy tasks are also handled by the 4th entry of nudge_messages (wording that makes it split the information and think step by step).'
    ),
    'thinking_loop_guard.nudge_messages': (
        'Warning messages injected after a loop is detected (specified as a JSON/Python-style list (array)).\n'
        'The 1st is used for the 1st retry, the 2nd for the 2nd, and so on. If there are more retries than messages, they are chosen randomly after that (so that repeating the same wording does not induce the same circular reasoning). An empty list [] uses the built-in default messages.'
    ),
    'thinking_loop_guard.empty_response_max_retries': (
        'Maximum number of retries, urging a final answer, when a silent end (an AIMessage with neither tool_calls nor a body) is detected (shared by the main graph and the eval harness; it used to be hard-coded in app.py, but it lives here as the sibling setting paired with max_retries above). Consumed independently of max_retries above (the total number of attempts is at most the sum of both + 1). Local models sporadically write the tool call XML into the body and end up with an empty response, and retrying often fixes it.\n'
        'Empty responses sometimes come several times in a row right after tool results that only check state, such as create_plan/approve_plan/update_task_progress, and each retry is cheap (retrying from an empty response completes immediately), so it has been increased to leave margin for consumption early on.'
    ),
    'tool_loop_guard.enabled': (
        'Settings of the feature that detects loops repeating exactly the same response (the body and the names/arguments of tool calls) consecutively (src/tool_loop_guard.py). Small local models sometimes repeat exactly the same response even after reading tool results (especially errors) (in an eval, one produced the same response 30 times in a row until it used up the token limit). Tool results and whether they are errors are not compared. Thinking varies every time, so it is not compared.\n'
        'When detected, it recovers with the same mechanism as [thinking_loop_guard]: it retries with a warning message from nudge_messages plus "what was repeated", and when max_retries (combined with thinking loops) is reached it stops and notifies the user. Sub-agents return their progress so far and stop when the limit is reached.\n'
        'Before retrying, the repeated responses and their results are removed from the conversation history (if left, the model keeps copying the same call from the history even after reading the warning).\n'
        'Also, calls whose arguments leak tool_call syntax (</parameter>, etc.) are always not run and return an error, regardless of this setting.\n'
        'Forcing tool calls on the model (tool_choice) does not work with Qwen3.6, so it is not used.'
    ),
    'tool_loop_guard.max_repeats': (
        'How many consecutive identical responses count as a loop. 1 or less disables it.'
    ),
    'tool_loop_guard.exclude_tools': (
        'Responses that call only these tools are not counted (tools that legitimately repeat the same call, like polling to check state; list form).'
    ),
    'tool_loop_guard.nudge_messages': (
        'Warning messages injected after a loop is detected (specified as a JSON/Python-style list (array)).\n'
        'The 1st is used the 1st time, the 2nd the 2nd time, and so on (counted separately from thinking loops); if there are more than prepared, they are chosen randomly after that. "What was repeated how many times" and an excerpt of its result are automatically appended to the end of each message. An empty list [] uses the built-in default messages.'
    ),
    'thinking_control.enabled': (
        'Settings of the feature that, at each step of the ReAct loop, mechanically chooses the thinking level from the shape of the preceding history and switches it per request (src/llm/thinking_control.py). The aim is to make responses faster by suppressing thinking on routine steps, such as right after a successful Read/Grep.\n'
        'There are 5 levels: off / low / medium / high / xhigh. off is enable_thinking=false; the others are expressed by the size of the thinking budget (llama-server: reasoning_budget_tokens / vLLM: thinking_token_budget). Neither changes the beginning of the prompt, so the KV cache is not missed.\n'
        "[llm].reasoning_effort is not switched per step (Qwen3.6's official template does not read it, and templates that support it add instructions to the system prompt, so switching it would miss the cache).\n"
        'Turns the whole feature on/off. If false, the requests are exactly the same as before. It does not work even if true when [llm].enable_thinking = false or reasoning_effort = none.'
    ),
    'thinking_control.apply_to_main': (
        'Whether to apply it to the loop of the main agent / sub-agents (dispatch_agent). It is never applied to LLM calls for summarizing/compaction.'
    ),
    'thinking_control.apply_to_sub': (
        'Whether to apply it to the loop of sub-agents (dispatch_agent). If false, sub-agent requests do not switch the thinking level.'
    ),
    'thinking_control.rule_user_turn': (
        'For rule_*, write the level (off / low / medium / high / xhigh) used when the rule applies. false disables that rule, and the next rule decides. If all are false, default_level is always used.\n'
        'Rule 1: when the last message is a user message (including nudges, warnings on loop detection and the token guard). Image-only messages added afterward as the result of a tool that read an image are treated as a continuation of the tool result, not as user messages.'
    ),
    'thinking_control.rule_tool_error': (
        'Rule 2: when the previous tool result is an error (ToolMessage.status="error", or the body starts with one of error_prefixes). To make it think about the cause of the error, default_level or higher is the norm. The xhigh budget is unlimited by default, so use xhigh only if unlimited thinking on every error is acceptable.'
    ),
    'thinking_control.rule_consecutive_cap': (
        'Rule 3: when the previous max_consecutive_reduced steps were all below this level (a safety valve against drifting off course while thinking little). Normally the same as default_level.'
    ),
    'thinking_control.rule_after_tools': (
        'Rule 4: when all the tools called in the previous step are in after_tools (if even one tool not in the list is mixed in, go to rule 5). To suppress thinking on routine steps such as right after a successful Read/Grep, lower than default_level is the norm.'
    ),
    'thinking_control.default_level': (
        'Rule 5: when no rule applied.'
    ),
    'thinking_control.budget_low': (
        'Thinking budget of each level (number of tokens, -1 = unlimited). If empty, [llm].reasoning_budget is used. To eliminate thinking, use the off level, not 0 (llama-server ignores 0 in requests).\n'
        'Note: when thinking is cut off by the budget, the rest of the thinking may leak into the body (see the comment of [llm].reasoning_budget). The smaller the budget, the more cutoffs, so lower it while checking with evals.\n'
        'On vLLM, using the budget (thinking_token_budget) requires specifying --reasoning-parser at startup (otherwise it is refused with HTTP 400; the budgets here are sent even if [llm].reasoning_budget is empty). Also, a bug has been reported where the end-of-thinking string is inserted in the middle of tool call arguments with Qwen3.5 and later (vLLM issue #44676). If you make the budget small, check that you have a fixed version.'
    ),
    'thinking_control.budget_medium': (
        'Thinking budget of the medium level (number of tokens, -1 = unlimited; if empty, [llm].reasoning_budget).'
    ),
    'thinking_control.budget_high': (
        'Thinking budget of the high level (number of tokens, -1 = unlimited; if empty, [llm].reasoning_budget). With the default empty value, it is the same budget as normal thinking.'
    ),
    'thinking_control.budget_xhigh': (
        'Thinking budget of the xhigh level (number of tokens, -1 = unlimited; if empty, [llm].reasoning_budget). Unlimited by default.'
    ),
    'thinking_control.after_tools': (
        'List of tools covered by rule 4 (list form). A warning is given at startup if a tool name does not exist.'
    ),
    'thinking_control.error_prefixes': (
        'Rule 2: a tool result is considered an error if it starts with one of these strings (several allowed in list form).'
    ),
    'thinking_control.max_consecutive_reduced': (
        'Rule 3: when levels below rule_consecutive_cap continue this many times, go back to rule_consecutive_cap. 0 or less never goes back.'
    ),
    'paths.common_data_dir': (
        "Common base directory for the various data storage paths. Writing ${common_data_dir} in the values of checkpoint_db/memory_dir/plans_dir under [paths], and of the dir keys of other sections ([log]/[default_workdir]/[path_memory]/[chat_log]), replaces it with the value given here (not configparser's standard interpolation but a custom string replacement done by load_config() in src/config.py at startup; a mechanism that works across sections). To change the storage root all at once, change only this.\n"
        '${instance} is replaced with the name of the running instance (the instance name when started from the admin tool, default when started directly with app.bat; can be specified with the environment variable LOCOHANE_INSTANCE). With the default ./data/${instance}, data (threads, logs, persistent memory, etc.) is automatically separated per instance into data/<instance name>/. ${instance} can be used in common_data_dir itself and in each of the keys above that accept ${common_data_dir}.\n'
        "Can be overridden with the corresponding environment variable COMMON_DATA_DIR (the environment variable's value is not subject to expansion of the ${common_data_dir} placeholder; give a plain path directly. ${instance} is expanded)."
        ' Local disk only (UNC paths and network drives are rejected at startup and when saving on the dashboard, because SQLite fails with "database is locked" on a network share). Back up to a file share with [data_backup] instead.'
    ),
    'paths.skills_dir': (
        'Skill definitions folder (the SKILL.md of each subfolder under it is scanned at startup).'
    ),
    'paths.agents_dir': (
        "Agent type definitions folder (on a par with skills_dir; equivalent to Claude Code's .claude/agents/*.md). Each *.md under it is scanned at startup as an agent_type of dispatch_agent.\n"
        '1 file = 1 type. Specify name/description/tools in the frontmatter (when tools is omitted, the default set of tools is inherited).'
    ),
    'paths.project_locohane_dir': (
        "Project-specific extension directories (equivalent to Claude Code's .claude/).\n"
        'The following 3 things under each given directory are detected automatically at startup (none of them causes an error if missing):\n'
        'skills/      - additional skills merged into the scan of skills_dir (if a skill with the same name exists in both, this side takes precedence)\n'
        'agents/      - additional agent types merged into the scan of agents_dir (if a definition with the same name exists in both, this side takes precedence)\n'
        "LOCOHANE.md  - project-specific additional instructions inserted into {{project_instructions}} of the system prompt (equivalent to Claude Code's CLAUDE.md).\n"
        'No error if missing ("(no project-specific instructions)" is just inserted). Files over 200 lines are truncated.\n'
        'To specify several directories, use the same JSON/Python-style list as [thinking_loop_guard].nudge_messages (square brackets, multiple lines OK). Later directories in the list take precedence (skills/agents are merged, and LOCOHANE.md files are concatenated with headings; no merging or deduplication). Example:\n'
        'project_locohane_dir = [\n'
        '"./.locohane",\n'
        '"./.locohane_team",\n'
        ']\n'
        'If you write an element as a dict {"dir": directory, "python": Python executable}, only the .py scripts in skills/ under that directory (via run_script/run_script_background) are started with that dedicated Python environment (takes precedence over [scripts].python; when python is omitted, or for plain string elements, [scripts].python is used as before). python is resolved as a path relative to the project root only if it contains a path separator; otherwise (a command name such as "python") it is used as is. The execute_python_code tools are not covered. Example:\n'
        'project_locohane_dir = [\n'
        '"./.locohane",\n'
        '{"dir": "./.locohane_team", "python": "./.locohane_team/.venv/Scripts/python.exe"},\n'
        ']'
    ),
    'paths.bin_path': (
        'Directories added to the front of the PATH passed to the subprocesses of run_script/execute_python_code (where external binary executables are placed). Skills that call a bare command name fail with "command not found" unless the user has registered it manually in the OS PATH environment variable. Stating the location here makes it callable without manual registration both in evals and when running app.py (see _subprocess_env in src/tools.py).\n'
        'Specify it in the same JSON/Python-style list form as project_locohane_dir. Directories that do not exist are ignored.'
    ),
    'paths.system_prompt_path': (
        'Template file of the system prompt (the skill list is inserted into {{skills}}).'
    ),
    'paths.checkpoint_db': (
        "SQLite file that stores LangGraph's conversation state (checkpoints)."
        ' Local disk only (UNC paths and network drives are rejected at startup and when saving on the dashboard, because SQLite fails with "database is locked" on a network share). Back up to a file share with [data_backup] instead.'
    ),
    'paths.memory_dir': (
        'Storage location of persistent memory (has the user/feedback/project/reference subfolders and the MEMORY.md index).'
    ),
    'paths.plans_dir': (
        'Storage location of the detailed Markdown files of execution plans written when create_plan is passed the detail_markdown argument. One file per conversation thread, overwritten every time create_plan is called (an optional feature, so no error even if empty).'
    ),
    'paths.help_path': (
        'Help text (for users, Markdown) that the help tool loads and returns.'
    ),
    'checkpointer.op_timeout_seconds': (
        "Timeout settings around the SQLite connection (aiosqlite, [paths].checkpoint_db) that persists LangGraph's conversation state. Introduced as a countermeasure for DB disconnection errors (no active connection).\n"
        'Timeout in seconds per checkpointer operation (aget_tuple/aput_writes, etc.).\n'
        "If aiosqlite's background thread gets stuck, its internal lock is never released and every operation hangs, so it gives up after this many seconds, raises _CheckpointerTimeout, and falls back to rebuilding the checkpointer (see also the 2 values below) (see _TimeoutGuardedSqliteSaver._guarded in app.py)."
    ),
    'checkpointer.close_timeout_seconds': (
        'Timeout in seconds for closing the old DB connection when rebuilding the checkpointer.\n'
        'Even if closing the old connection gets stuck, switching to the new connection itself is not blocked (when exceeded, it only logs and tolerates the leak; see _rebuild_checkpointer in app.py).'
    ),
    'checkpointer.shutdown_drain_timeout_seconds': (
        'Maximum seconds to wait at app shutdown for pending asynchronous tasks (on_message stream processing, etc.) to finish. Tasks still remaining after this many seconds are canceled, then their cleanup is awaited, and only then is the DB connection closed. Closing the connection first would make the checkpointer operations of pending tasks fail with ValueError: no active connection, so this order is always kept (see _close_checkpointer_gracefully in app.py).'
    ),
    'chainlit_files.retention_days': (
        "Automatic deletion settings for Chainlit's own session file directory (.files/<session ID>/ at the project root; the path is managed by Chainlit and cannot be changed here), used when delivering to the browser the automatic preview attachments of generated files (cl.Image, including the show_in_chat=True argument of analyze_image) and images embedded in answer text (_embed_local_images_as_session_urls in app.py). Chainlit itself also deletes files when the browser disconnects, but that does not always work reliably, e.g. on process restarts, so day-based automatic deletion is provided as insurance, as for the other data/* folders.\n"
        'Unlike the other sections, it deletes per directory (per session ID).'
    ),
    'chainlit_files.cleanup_interval_hours': (
        'Interval (hours) of the automatic deletion check. The check also runs once at startup.'
    ),
    'elements.dir': (
        'Storage location that persists attachments such as provide_download/analyze_image(show_in_chat=True) and images embedded in answer text, so that they can still be shown after resuming a thread or restarting the process (see create_element/get_element in src/thread_store.py and _embed_local_images_as_session_urls in app.py).\n'
        'Separately from the temporary session delivery of [chainlit_files], the actual files are copied and saved here.'
    ),
    'elements.retention_days': (
        'Retention in days. Files older than this (by modification time) are deleted automatically.\n'
        '0 or less disables automatic deletion.\n'
        'The default is 0 (disabled). The default of [thread_store].retention_days is also 0 (threads kept indefinitely), so automatically deleting by days only here would make only the attachments and embedded images of old threads still in the list (not deleted) disappear first, leaving broken links (404). Deleting files when a thread is deleted is done individually by delete_thread_row/cleanup_old_threads in thread_store.py, so normally there is no need to change this value. Set it only when you want to thin out files more aggressively than the lifetime of threads, e.g. when operating with [thread_store].retention_days enabled.'
    ),
    'elements.cleanup_interval_hours': (
        'Interval (hours) of the automatic deletion check. The check also runs once at startup.'
    ),
    'images.max_long_side_pixels': (
        'Upper limit on the long side in pixels when shrinking images before passing them to the LLM (applies to images loaded by the analyze_image tool; images uploaded by users are also shrunk when the LLM opens them with analyze_image).\n'
        'It is given as the long side in pixels rather than "DPI" because JPEGs taken with smartphones have no / unreliable DPI metadata, while the number of tokens a vision model consumes is determined almost entirely by the number of pixels. Lower values increase the number of images one sub-agent delegation can handle, but make small text in photos harder to read.\n'
        '0 means no shrinking (the original resolution is sent as is).\n'
        'If the long side of an image is already at or below this value, it is not re-encoded and the original data is used as is.'
    ),
    'images.jpeg_quality': (
        'Quality (1-95) of the JPEG re-encoded after shrinking. For reading images that contain text, too low makes it illegible, so 85 or higher is recommended.'
    ),
    'images.inline_preview_max_long_side_pixels': (
        "Upper limit on the long side in pixels of image previews embedded directly in the LLM's answer text (Markdown table cells, etc.). Separate from max_long_side_pixels above (for vision); an intentionally small value (_embed_local_images_as_session_urls in app.py delivers them to the browser through the same session file route as analyze_image(show_in_chat=True) etc.; they are thumbnails in cells, so smallish is fine to save display bandwidth and disk usage)."
    ),
    'images.inline_preview_jpeg_quality': (
        'Re-encoding quality (1-95) of the thumbnails above.'
    ),
    'images.inline_preview_min_long_side_pixels': (
        'Lower limit on the long side in pixels of the thumbnails above. Small images around icon size become unrecognizable when embedded as is, so images whose long side is smaller than this are enlarged to this value before embedding. 0 means no enlarging (keep the original size; default). Make it at most max_long_side_pixels.'
    ),
    'default_workdir.dir': (
        "The agent's default working directory (the cwd of run_script and the base of the output location of files generated by scripts). Used when no per-session working directory is specified with the working folder icon.\n"
        'In fact, a thread-only folder _tmp_<creation time>_<thread ID>/ is created right under it, and that becomes the working directory. Files uploaded in the chat are also always saved in uploads/<send time>/ of this thread-only folder, regardless of the working folder setting (other threads cannot read them).'
    ),
    'default_workdir.retention_days': (
        'Retention in days of files that keep accumulating right under dir above. Files older than this (by modification time) are deleted automatically. 0 or less disables automatic deletion. Per-session work_dirs specified by users in ChatSettings are not covered (only dir is).\n'
        'Thread-only folders (_tmp_*) are also deleted entirely, including uploaded files, once this many days have passed since they were last modified. Uploaded images are not embedded directly in the conversation; only the saved path is passed to the LLM, so if you resume an old thread after this many days, the images and files uploaded in that thread can no longer be opened (upload them again if needed).'
    ),
    'default_workdir.cleanup_interval_hours': (
        'Interval (hours) of the automatic deletion check. The check also runs once at startup.'
    ),
    'default_workdir.allow_sandbox_dir': (
        'Additional directories where the write sandbox guard allows writing and deleting, besides dir above (default_workdir). For registering fixed shared output locations prepared by operators, etc. (empty by default; the write sandbox principle itself does not change).\n'
        'Only via run_script/run_script_background. execute_python_code/execute_python_code_background (tools that run code freely generated by the LLM on the spot) are not covered and always stay limited to work_dir/default_workdir as before (because they are riskier).\n'
        'Each element is a dict {"dir": directory, "allow_entries": [...]}.\n'
        'Several can be given in JSON/Python-style list form (square brackets, multiple lines OK, trailing comma allowed; the same notation as [thinking_loop_guard].nudge_messages).\n'
        'dir          : the directory to allow (relative paths are relative to this file; absolute paths are also OK). Directories that do not exist are ignored.\n'
        'allow_entries: the list of targets allowed to write to that directory.\n'
        'Same format as [main_agent_tool_guard].allow_entries; each element is one of these 2 kinds:\n'
        '- "tool name" (a built-in tool name; currently the only built-in tools with the write sandbox guard are run_script/run_script_background, so it is effectively unused, but the format is aligned for future extension)\n'
        '- ["skill name","script file name"] (allows only skill scripts called via run_script/run_script_background)\n'
        'An empty list allows unlimited writing regardless of the target (anyone\n'
        'can write).\n'
        'Note that registered directories are always writable regardless of session or thread (the per-session separation of work_dir/default_workdir does not apply).\n'
        'Example:\n'
        'allow_sandbox_dir = [\n'
        '{"dir": "E:/shared_output", "allow_entries": [["excel-edit", "write_excel.py"]]},\n'
        ']'
    ),
    'skill_creator.draft_dir': (
        'Handling of draft skills (unverified skills) created by the skill-creator skill (see src/skill_drafts.py).\n'
        'Drafts are created in <draft_dir>/<username>/<skill name>/ and appear in the skill list as "<username>/<skill name>" only in conversations of the user who created them (a new skill can be used from the message after it was created with skill-creator).\n'
        "The scripts of a user's own drafts can be run directly from the main agent without plan approval, regardless of the [plan].plan_approval_exempt_scripts and [main_agent_tool_guard] settings (because the creators are not necessarily skill development experts; the write destination is limited to the working directory as usual).\n"
        "To become official skills, a skill developer finishes them in the Skill Lab of the admin tool ([skill_lab]), and only those that pass the skill stabilization tryout are promoted to the location dedicated to the instance that created the draft ([skill_lab].instance_locohane_dir) (they can also be promoted with the same rules by Claude Code's promote-skill skill). When a draft is modified, its pass count so far goes back to 0. Promoted drafts are moved to archive_dir.\n"
        "The scripts/*.py of promoted skills are added to [plan].plan_approval_exempt_scripts and [main_agent_tool_guard].allow_entries (max_calls=-1) in that instance's config_overrides.json so that they behave the same as when they were drafts (the tryout is also evaluated with the same settings as if already added).\n"
        'The draft location. Making it the same as, or nested with, the startup skill scan targets ([paths].skills_dir / agents_dir / project_locohane_dir) causes an error at startup (to prevent drafts from being published to all users). ${common_data_dir} / ${instance} can be used.'
    ),
    'skill_creator.archive_dir': (
        "Where promoted drafts are moved (moved together with _workspace to <username>/<skill name>_<date time>/; they disappear from the creator's list). Like draft_dir, making it the same as, or nested with, the skill scan targets or draft_dir causes an error at startup. ${common_data_dir} / ${instance} can be used."
    ),
    'skill_creator.tryout_repeats': (
        'Default number of runs of the skill stabilization tryout (evals/run_all.py --repeat). On promotion, each case is repeated this many times, and only those that pass every run pass. An integer of 1 or more.'
    ),
    'skill_creator.other_users_drafts': (
        "Handling of other users' drafts (everything is always possible for your own drafts). One of these 4 values:\n"
        'hidden   - not shown in the skill list. Cannot be read, run, written or evaluated (cannot be read from Read/Glob/Grep/execute_python_code etc. either).\n'
        'listed   - shown in the skill list (noted as unusable). Cannot be read, run, written or evaluated.\n'
        'readable - shown in the skill list and can be read. Cannot be run, written or evaluated.\n'
        'full     - can be listed, read, run, written and evaluated.'
    ),
    'skill_lab.instance_locohane_dir': (
        'Settings of the Skill Lab ("Skill Lab" tab of the admin tool, admin/skill_lab/) that apply on the side of the research theme\'s target instance (the promotion destination). Override them in the main instance\'s config_overrides.json.\n'
        'For the settings of the skill tuning worker itself, see [skill_lab_worker].\n'
        'Instance-only extension directory (the promotion destination of the Skill Lab and promote-skill). Promoted skills and sub-agents are placed in <here>/skills/ and agents/, and the set of regression test cases in <here>/evals/<theme ID>/.\n'
        'When the main instance starts, skills/, agents/ and LOCOHANE.md under it are also detected automatically like [paths].project_locohane_dir, and always scanned after (with priority over) all of project_locohane_dir (it is not dropped even if project_locohane_dir is overridden).\n'
        "project_locohane_dir is shared by all instances, but this is seen only by that instance, so instances per organization or role do not mix each other's skills (if it has the same name as a bundled skill, it is replaced only in that instance).\n"
        "${instance_dir} is the instance's settings folder ([admin].instances_dir/<instance name>, default instances/<instance name>); ${instance} and ${common_data_dir} can also be used.\n"
        "The default instances/<instance name>/ is deleted together with the instance (the admin tool's delete dialog warns about it)."
    ),
    'skill_lab_worker.max_iterations': (
        'Settings of the skill tuning worker itself (an instance whose kind is set to "skill tuning worker" in the admin tool; admin/skill_lab/worker.py). Override them in the worker\'s config_overrides.json (they do not appear on the settings screen of main instances).\n'
        'The worker evaluates research themes (a bundle of skills, sub-agents and cases, placed in ${common_data_dir}/skill_lab/themes/ of the target instance), and runs a loop in which the AI fixes failures and evaluates again.\n'
        "Evaluation, AI fixes, judging and drafting inherit the settings of the research theme's target instance as they are (skills, guards, timeouts, the inference parameters of [llm], etc.).\n"
        "The worker has only these of its own: the LLM endpoints ([llm] main_url / sub_url / main_routing_strategy / sub_routing_strategy), the loop limits of this section, and the worker's own logs ([log]) (only these appear on the worker's settings screen in the admin tool too).\n"
        'Maximum number of iterations of the skill tuning loop (1 iteration = 1 evaluation + 1 fix on failure). When exceeded, it stops with "limit reached" and waits for a human decision.'
    ),
    'skill_lab_worker.max_hours': (
        'Maximum time (hours, decimals allowed) one theme\'s loop may use. When exceeded, it stops with "limit reached".'
    ),
    'skill_lab_worker.poll_interval_seconds': (
        'Interval (seconds) at which the worker looks for themes waiting to be processed.'
    ),
    'skill_lab_worker.fixer_max_file_chars': (
        'Maximum number of characters per asset file passed to the AI when asking for a fix (the excess is omitted).'
    ),
    'skill_lab_worker.transcript_excerpt_chars': (
        'Maximum number of characters of the transcript excerpt (tool calls and results) passed to the AI when asking for a fix or judging (per run).'
    ),
    'path_memory.dir': (
        'Directory where path memory (src/path_memory.py) saves the per-conversation registry files (<thread_id>.json).'
    ),
    'path_memory.retention_days': (
        'Retention in days. Registry files older than this (by modification time) are deleted automatically.\n'
        'There is no way to detect "end of conversation" explicitly, so it is managed by days.'
    ),
    'path_memory.cleanup_interval_hours': (
        'Interval (hours) of the automatic deletion check. The check also runs once at startup.'
    ),
    'path_memory.max_entries': (
        'Maximum number of registrations per conversation.'
    ),
    'path_memory.search_min_score': (
        'Lower limit of the similarity (0.0 to 1.0) used by the search_path_memory tool and by the candidates presented on "not found" errors. Registrations below this are not shown in results.\n'
        'Higher values select candidates more strictly; lower values are more tolerant of typos and spelling variations (unrelated paths are more likely to be mixed in).'
    ),
    'path_memory.search_filename_weight': (
        'When the search term is a full path, the weight (0.0 to 1.0) given to the file name part in the similarity calculation. The rest is made up by the similarity between the folder parts.\n'
        'Example: 0.7 means "similarity of the file names × 0.7 + similarity of the folders × 0.3". Not used when the search term is only a file or folder name.'
    ),
    'vector_db.enabled': (
        'Enables/disables the vector DB (semantic search of internal documents, src/vector_db/) (true/false).\n'
        'true makes the search_documents / list_document_collections tools available. Documents are ingested on the "Knowledge" tab of the settings dashboard.'
    ),
    'vector_db.backend': (
        'Where vectors are stored. One of these 2 values:\n'
        'sqlite_vec - store in vectors.sqlite under dir below (no server needed; default).\n'
        'It has no nearest-neighbor index and compares against all chunks on every search, so search slows down in proportion to the count.\n'
        'Guideline for the number of chunks it can handle (total of all collections in the instance; for the 1024-dimensional bge-m3):\n'
        'up to 100,000  … comfortable (about 0.1-0.3 seconds per search).\n'
        'up to 300,000  … the practical limit (about 1 second per search; vectors.sqlite is about 1.2GB).\n'
        'more than that … use qdrant.\n'
        'qdrant     - store in the Qdrant server at qdrant_url below (searches with an HNSW index, so search stays fast as the count grows).\n'
        "Guideline for the number of chunks it can handle: up to 1 million with qdrant_preset = none (Qdrant's default)\n"
        '(all vectors are kept in RAM, so about 4GB of RAM is needed per 1 million at 1024 dimensions),\n'
        'and 1 to 10 million with qdrant_preset = 1m-10m. Beyond 10 million, consider a Qdrant cluster (sharding).\n'
        'With chunk_size = 800, the number of chunks is roughly 1-2 per densely written A4 page. Page images with image_input = true\n'
        'go into a separate collection (<name>.image), one per page, so add those to the count too.'
    ),
    'vector_db.distance': (
        '[Note] After changing distance, "rebuild" all existing collections on the Knowledge screen (the DB needs to be recreated).\n'
        'The distance function used when creating collections (how closeness between vectors is measured). One of these 4 values (case-insensitive):\n'
        'Cosine    - cosine similarity (default). Compares only the direction of vectors. Many embedding models (bge-m3, etc.) assume this.\n'
        'Euclid    - Euclidean distance (straight-line distance; l2 in sqlite-vec). Smaller is closer.\n'
        'Manhattan - Manhattan distance (sum of the absolute differences in each dimension; l1 in sqlite-vec). Smaller is closer.\n'
        'Dot       - dot product (only with backend = qdrant; sqlite-vec does not support it, and specifying it causes an error at startup).\n'
        'For normalized vectors it gives the same ranking as Cosine. Values may not fall within 0.0-1.0.\n'
        'To align Euclid and Manhattan to "larger is closer", the similarity in search results is converted to 1 / (1 + distance) before\n'
        'being compared with min_score, image_min_score and path_min_score (the values are not the same as Cosine, so adjust the lower limits for the model you use).\n'
        'The distance function is fixed when a collection is created and is not applied to collections created before. Running a differential ingestion after changing it without rebuilding\n'
        'gives an error saying the distance differs and to rebuild it (searches keep using the distance function it was created with).'
    ),
    'vector_db.dir': (
        'Directory where collection definitions and records of ingested files (catalog.sqlite), and with backend=sqlite_vec the vectors (vectors.sqlite), are stored.\n'
        'catalog.sqlite is placed here even with backend=qdrant.'
        ' Local disk only (UNC paths and network drives are rejected at startup and when saving on the dashboard, because SQLite fails with "database is locked" on a network share). Back up to a file share with [data_backup] instead.'
    ),
    'vector_db.qdrant_url': (
        'URL of the Qdrant server with backend=qdrant (e.g. http://localhost:6333). If an API key is needed, write it in QDRANT_API_KEY in .env.\n'
        'To use it completely offline, specify a local or in-LAN server.'
    ),
    'vector_db.qdrant_collection_prefix': (
        'Prefix added to collection names on Qdrant. ${instance} is replaced with the instance name. Vary it per instance so that they do not mix even when sharing the same Qdrant server.\n'
        'Qdrant collections that do not start with this prefix are never touched.'
    ),
    'vector_db.qdrant_timeout_seconds': (
        'Timeout (seconds) per request to Qdrant. Ingestion waits until writes are applied (wait=true),\n'
        'so it can take time while indexes are being rebuilt or with large collections.\n'
        'Searches (search_documents, etc.) wait at most 30 seconds regardless of this value so that the AI\'s response is not held up.\n'
        'Connecting (until the Qdrant host is reached) is cut off after at most 10 seconds regardless of this value (so a down host is not waited on for long).'
    ),
    'vector_db.qdrant_max_retries': (
        'Number of times the same request is sent again after waiting qdrant_retry_interval_seconds seconds when Qdrant cannot be reached,\n'
        'closes the connection, times out or returns a temporary error (5xx, 408, 429). -1 retries without limit until it succeeds\n'
        '(for waiting for Qdrant to restart; ingestion and copying can be stopped with "Stop" on the Knowledge screen). 0 does not retry.\n'
        '4xx other than 408 and 429 (mistakes in the request or settings that waiting does not fix) are not retried and give an error.\n'
        'Searches (search_documents, etc.) and collection deletion cannot be stopped and would not respond, so they are retried at most once (with an interval of at most 1 second) regardless of this value.\n'
        'Shared by all instances; change it on the "Admin settings" screen of the settings dashboard (it cannot be changed in config_overrides.json per instance).'
    ),
    'vector_db.qdrant_retry_interval_seconds': (
        'Interval (seconds) between the retries above.\n'
        'Shared by all instances; change it on the "Admin settings" screen of the settings dashboard (it cannot be changed in config_overrides.json per instance).'
    ),
    'vector_db.qdrant_preset': (
        'With backend = qdrant, Qdrant collection settings matched to the expected scale of the number of chunks (body text or images of one collection). One of these 11 values:\n'
        "none - specify nothing (Qdrant's default; default value). All vectors are kept in RAM. For up to 1 million.\n"
        '1m   - for up to 1 million. HNSW m=16, ef_construct=128, hnsw_ef=128 when searching. Vectors in RAM.\n'
        'RAM guideline about 6GB (server 8GB or more).\n'
        '2m   - for up to 2 million. m=16, ef_construct=200, hnsw_ef=160. Original vectors on disk (mmap).\n'
        'RAM guideline about 3GB + disk cache (server 8GB or more).\n'
        '3m   - for up to 3 million. m=24, ef_construct=256, hnsw_ef=200. Original vectors on disk.\n'
        'RAM guideline about 4GB + disk cache (server 16GB or more).\n'
        '4m   - for up to 4 million. m=32, ef_construct=256, hnsw_ef=256. Original vectors on disk.\n'
        'RAM guideline about 5GB + disk cache (server 16GB or more).\n'
        '5m   - for up to 5 million. m=32, ef_construct=320, hnsw_ef=288. Original vectors on disk.\n'
        'RAM guideline about 6GB + disk cache (server 16GB or more).\n'
        '6m   - for up to 6 million. m=40, ef_construct=320, hnsw_ef=320. Original vectors on disk.\n'
        'RAM guideline about 8GB + disk cache (server 32GB or more).\n'
        '7m   - for up to 7 million. m=40, ef_construct=384, hnsw_ef=352. Original vectors on disk.\n'
        'RAM guideline about 9GB + disk cache (server 32GB or more).\n'
        '8m   - for up to 8 million. m=48, ef_construct=384, hnsw_ef=384. Original vectors on disk.\n'
        'RAM guideline about 11GB + disk cache (server 32GB or more).\n'
        '9m   - for up to 9 million. m=48, ef_construct=448, hnsw_ef=416. Original vectors on disk.\n'
        'RAM guideline about 13GB + disk cache (server 32GB or more).\n'
        '10m  - for up to 10 million. m=64, ef_construct=512, hnsw_ef=448. Original vectors on disk.\n'
        'RAM guideline about 15GB + disk cache (server 32GB or more).\n'
        'All of 1m-10m search a copy of the vectors scalar-quantized to int8 kept in RAM, and rescore the top candidates with the original vectors\n'
        '(oversampling 1.5-3.0x). Chunk text (payload) is kept on disk. The number of segments is 1m=4, 2m=6, 3m/4m=8, 5m/6m=10, 7m/8m=12, 9m/10m=16.\n'
        'The RAM guidelines are for 1024 dimensions (bge-m3) and grow in proportion to the dimensions (about double for 2048). Disk is about 8GB per 1 million (SSD recommended).\n'
        'Existing collections are updated to the new settings when the next "ingest (differential)" starts (no re-embedding needed; Qdrant rebuilds the index and quantization\n'
        'in the background, and search keeps working meanwhile). Going back to none does not revert the settings of existing collections (rebuild to revert).'
    ),
    'vector_db.embedding_url': (
        'Endpoints of the inference server that computes embeddings. A list in the same format as [llm] main_url (calls the OpenAI-compatible /v1/embeddings).\n'
        'Example: start llama-server on another port with --embedding --pooling mean etc., and [{"base_url": "http://localhost:12440/v1", "api_key": "dummy-not-used", "model": "bge-m3", "provider": "llama_cpp"},]\n'
        '  provider: "openai_compatible" (default, can be omitted) / "llama_cpp" / "vllm". The kind of inference server. With round_robin / priority_failover it decides\n'
        '            how to check whether the endpoint is free before sending (see embedding_routing_strategy below; "openai_compatible" is not checked).\n'
        'If several are given, they are tried in the order of embedding_routing_strategy, switching to the next endpoint when an error is returned (cannot connect, non-200 HTTP).\n'
        'They must return the same vectors, so use the same model for all (start/end cannot be specified).\n'
        'Required when enabled = true ([] means not set).'
    ),
    'vector_db.embedding_routing_strategy': (
        'How to choose when there are several embedding_url entries. The same 3 values as [llm] main_routing_strategy:\n'
        'round_robin       : rotate the first endpoint one by one per request (default; for spreading the load over several machines with ingest_workers set to 2 or more).\n'
        'random            : choose randomly per request.\n'
        'priority_failover : always use the first one, and put an endpoint that returned an error at the back for 60 seconds and use the next one.\n'
        'With round_robin and priority_failover, it checks whether an endpoint is free before sending and moves on to the next one if it is full (checked even with a single endpoint):\n'
        '  provider = "llama_cpp" : counts the free slots with GET /slots (requests this process is sending also count as busy).\n'
        '  provider = "vllm"      : treated as full if vllm:num_requests_waiting (the queue) of GET /metrics is 1 or more.\n'
        '  Other providers, and cases where it cannot be checked, are treated as free.\n'
        'When all endpoints are full, ingestion waits until one is free (it can be stopped with "Stop"). Waiting requests use free slots in the order they started waiting,\n'
        'and are rechecked as soon as a request of this process returns, otherwise every [llm] round_robin_busy_poll_interval_seconds seconds (at most 0.5 seconds because an embedding is short).\n'
        'Search sends to the first in the order without waiting. The timeout of the check is [llm] round_robin_slots_probe_timeout_seconds. random does not check.\n'
        'Whichever you choose, when an error is returned the same request is resent to the next endpoint.'
    ),
    'vector_db.embedding_max_retries': (
        'When all endpoints return errors, the number of times to wait embedding_retry_interval_seconds seconds and then try all endpoints again.\n'
        '0 repeats indefinitely until it succeeds (for when you want to wait for the embedding server to restart; ingestion can be stopped with "Stop" on the Knowledge screen).\n'
        'Used only for ingestion (embedding documents); search follows search_embedding_max_retries.\n'
        'If only 4xx errors other than 408/429 are returned (mistakes in input or settings that waiting will not fix), it errors out without retrying.'
    ),
    'vector_db.search_embedding_max_retries': (
        'Number of retries for search (embedding the search term of search_documents) when all endpoints return errors.\n'
        'Search cannot be stopped and the AI\'s response would never return, so 0 means "do not retry" (unlimited cannot be specified).'
    ),
    'vector_db.embedding_retry_interval_seconds': (
        'Interval (seconds) of the retries above (shared by ingestion and search).'
    ),
    'vector_db.embedding_batch_size': (
        'Number of chunks whose embeddings are computed together in one request. During ingestion, even when small files continue, chunks of several files\n'
        '(and file paths) are accumulated up to this number before sending.'
    ),
    'vector_db.embedding_timeout_seconds': (
        'Timeout (seconds) per embedding request.'
    ),
    'vector_db.embedding_encoding_format': (
        'How embeddings are received (encoding_format of /v1/embeddings).\n'
        'base64 : receive float32 bytes in base64 (default). The response is about 1/3-1/4 the size of numeric JSON, and parsing is faster.\n'
        'llama-server and vLLM support it. If an endpoint that does not support it returns 400/422, only that endpoint switches to float automatically.\n'
        'float  : receive arrays of numbers (JSON).'
    ),
    'vector_db.query_prefix': (
        'String (prefix) added to the beginning of the search term (the query of search_documents) when embedding it. Some embedding models were trained with\n'
        'markers for "this is a search term" / "this is a document", and search accuracy drops without them.\n'
        'The document side is document_prefix. Normally both can stay auto.\n'
        'auto … decided from the model name of embedding_url (the Knowledge screen shows the value actually added):\n'
        'multilingual-e5 family            → "query: "\n'
        'multilingual-e5-*-instruct        → "Instruct: …\\nQuery: "\n'
        'ruri-v3 family                    → "検索クエリ: "\n'
        'ruri (v1, v2) family              → "クエリ: "\n'
        'nomic-embed family                → "search_query: "\n'
        'Nemotron embedding family (names containing nemotron and embed) → "query: "\n'
        'others (bge-m3, etc.)             → nothing added\n'
        'empty … nothing added.\n'
        'any other value … that string is added (for models not in the table, or when the model name differs). To keep trailing spaces,\n'
        'enclose it in double quotes (e.g. "query: ").\n'
        'Changing it does not require a rebuild.'
    ),
    'vector_db.document_prefix': (
        'String (prefix) added to the beginning of the chunk text of ingested documents when embedding it. The search term side is query_prefix.\n'
        'Not added to page images with image_input = true. Normally it can stay auto.\n'
        'auto … decided from the model name of embedding_url (the Knowledge screen shows the value actually added):\n'
        'multilingual-e5 family            → "passage: "\n'
        'multilingual-e5-*-instruct        → nothing added\n'
        'ruri-v3 family                    → "検索文書: "\n'
        'ruri (v1, v2) family              → "文章: "\n'
        'nomic-embed family                → "search_document: "\n'
        'Nemotron embedding family (names containing nemotron and embed) → "passage: "\n'
        'others (bge-m3, etc.)             → nothing added\n'
        'empty … nothing added.\n'
        'any other value … that string is added (for models not in the table, or when the model name differs). To keep trailing spaces,\n'
        'enclose it in double quotes (e.g. "passage: ").\n'
        'The value used at ingestion is recorded per collection, and if it differs from the setting, the collection is treated as needing a rebuild\n'
        '(the same as when the model is changed).'
    ),
    'vector_db.chunk_size': (
        'Approximate number of characters per chunk when splitting documents. Splits preferentially at paragraph and line breaks so as not to exceed this number.'
    ),
    'vector_db.chunk_overlap': (
        'Number of characters overlapped between adjacent chunks (so that context is not cut off at the boundaries). Make it smaller than chunk_size.'
    ),
    'vector_db.include_extensions': (
        'Extensions to ingest (comma-separated). Supported formats: .pdf .docx .xlsx .xlsm .pptx .md .txt .csv\n'
        'Image files (.png .jpg .jpeg .gif .bmp .webp .tif .tiff .heic .heif) are embedded as images if image_input = true;\n'
        'if false, only the file path is registered without reading the content (they can be found by file and folder names with search_document_paths).\n'
        'Files whose text cannot be extracted (image-only PDFs, etc.) are not treated as failures either; only the file path is registered.'
    ),
    'vector_db.text_extensions': (
        'Extensions read as text like .txt, in addition to the supported formats (comma-separated; empty by default).\n'
        'For application-specific formats whose content is text (ASCII, etc.) (e.g. .step, .igs, AutoForm material files .mat).\n'
        'To ingest them, also add them to include_extensions. The character encoding is read in the same order as .txt: UTF-8 -> the OS ANSI code page -> CP932.\n'
        'Files whose content is binary (contains NUL) are not read; only the file path is registered (for applications that can also save the same extension in a binary format).\n'
        'Extensions of the supported formats (.pdf .docx .md, etc.) and image files are still read in their own way even if listed here.'
    ),
    'vector_db.max_file_mb': (
        'Size limit per file (MB). Larger files are not ingested, and the reason is listed in the failure list.\n'
        'Shared by all instances; change it on the "Admin settings" screen of the settings dashboard (it cannot be changed in config_overrides.json per instance).'
    ),
    'vector_db.image_input': (
        'Whether to input images to the embeddings (true/false).\n'
        'false (default): extract text from all formats, split it into chunks and embed it as text.\n'
        'true         : for the formats in image_input_extensions (default .pdf), in addition to the text chunks, also keep embeddings of each page\n'
        '(each sheet for Excel) rendered as an image, the same as the render skills (docx-render, etc.) (figures, tables and scanned PDFs become searchable too).\n'
        'Office files not in image_input_extensions (by default .docx .pptx .xlsx .xlsm) embed, in addition to the text chunks, each image (photo, figure)\n'
        'inside the file as an image (see embedded_images). .md .txt .csv are embedded as text only, as before.\n'
        'Image vectors go into a collection separate from the text (<name>.image), and search merges the text and image results by rank (see image_min_score).\n'
        'Image files (.png .jpg .heic, etc.) are also ingested, each embedded as one chunk (multi-page TIFFs per page).\n'
        'Image files have their EXIF rotation applied and are shrunk so that the long side is at most the long side of A4 rendered at image_dpi (1169px at 100 DPI).\n'
        'embedding_url needs an embedding model that supports image input (e.g. Qwen3-VL-Embedding started on llama-server with --embedding --mmproj).\n'
        'Rendering Office files as images needs Windows and Microsoft Office (Word / PowerPoint / Excel) (files that cannot be rendered are listed in the failure list).\n'
        'Page images are written as PNG to a temporary folder only during ingestion (even a 400-page PDF does not keep all pages in memory)\n'
        'and deleted once written (the source of search results is the original file and page position).\n'
        'Macros in Office files are not run, and password-protected files are listed in the failure list (see office_timeout_seconds).\n'
        'In the ingestion (differential) after switching, files of the affected formats are re-ingested with the new method.'
    ),
    'vector_db.image_input_extensions': (
        'Document formats whose page images are also embedded when image_input = true (comma-separated; choose from .pdf .docx .pptx .xlsx .xlsm; if empty, none are rendered).\n'
        'Formats not included are embedded as text only. Image files (.png .jpg .heic, etc.) are embedded as images whenever image_input = true, regardless of this setting.\n'
        'Office (.docx .pptx .xlsx .xlsm) is not included by default: exporting to PDF with Office takes the longest time in ingestion (and needs Windows and\n'
        'Microsoft Office), while the text, shapes, comments and chart titles are ingested as text and the images inside are ingested as images by embedded_images.\n'
        'In the ingestion (differential) after changing it, files of the formats whose embedding method changes are re-ingested (image vectors are deleted or added).'
    ),
    'vector_db.embedded_images': (
        'With image_input = true, whether to take out the images (photos, figures) inside Office files (.docx .pptx .xlsx .xlsm) not in image_input_extensions\n'
        'and embed each of them as an image (true/false). Office is not started; the images in the file are read as they are (fast).\n'
        'Only images placed in the body, slides and sheets are read (not logos in slide masters or headers), and the same image is embedded only once.\n'
        'The source location is the slide or sheet cell plus the name of the image in the file, such as "Slide 3 image (ppt/media/image2.png)" or\n'
        '"Sheet: Summary C5 image (xl/media/image1.png)". The excerpt of search results has the alt text and the surrounding text (slide text, paragraphs before and after).\n'
        'If false, Office files are embedded as text only. The alt text of images goes into the text as "[Image] ..." regardless of this setting.\n'
        'In the ingestion (differential) after switching, Office files are re-ingested.'
    ),
    'vector_db.embedded_image_min_px': (
        'Lower limit (px, 0 or more) of the width and height of images taken out by embedded_images. Images smaller in either (icons, bullet symbols,\n'
        'rules, etc.) are not embedded. 0 means images are not excluded by size. After changing it, rebuild to apply it to already ingested files.'
    ),
    'vector_db.image_dpi': (
        'Resolution (DPI) at which pages are rendered as images when image_input = true. Raising it captures finer text, but images get larger and embedding takes longer.'
    ),
    'vector_db.office_timeout_seconds': (
        'With image_input = true, the limit in seconds for one export of an Office file to PDF (a value greater than 0). When exceeded, it is considered stuck on an invisible dialog\n'
        '(password input, "a serious error occurred the last time it was opened", etc.), the process of that Office application (Word / PowerPoint /\n'
        'Excel) is terminated, and the file is listed in the failure list. If the time is not enough for documents of hundreds of pages, increase it.\n'
        'Shared by all instances; change it on the "Admin settings" screen of the settings dashboard (it cannot be changed in config_overrides.json per instance).'
    ),
    'vector_db.ingest_temp_dir': (
        'Folder for temporary files used only during ingestion (page image PNGs of image_input, images extracted by embedded_images, PDFs exported from Office).\n'
        'Empty (default) means the system temporary folder (%TEMP%). Relative paths are based on the project root (${common_data_dir} and ${instance} cannot be used).\n'
        'It is created if missing. A folder for each ingestion run (locohane_vdb_pages_*) is created under it and deleted when the ingestion ends.\n'
        'Page images of a PDF with hundreds of pages can take hundreds of MB, so specify another drive when the system drive is low on free space\n'
        '(avoid network drives, which are slow).\n'
        'Shared by all instances; change it on the "Admin settings" screen of the settings dashboard (it cannot be changed in config_overrides.json per instance).'
    ),
    'vector_db.ingest_workers': (
        'Ingestion parallelism (1 or more). The number of files prepared in parallel (reading, extracting text, rendering images; the value of ingest_processes when it is 1 or more).\n'
        'The number of embedding requests sent at the same time is set by ingest_embed_requests (0 means the same as this value).\n'
        '1 (default) prepares one file at a time.\n'
        'With 2 or more, larger files are started first (to avoid one large file being left at the end while everything else waits).\n'
        'For Office files with image_input = true, each worker starts one Word / PowerPoint / Excel and reuses it during ingestion,\n'
        'so increase it only within the memory you have to spare.\n'
        'Shared by all instances (cannot be changed per instance; the same goes for ingest_processes). Can be changed in "Admin settings" of the settings dashboard (the value is saved in <instances_dir>/admin_settings.json and takes precedence over this; it cannot be changed per instance). \n'
        'Ingestion from the settings dashboard runs\n'
        'at most this many in total across all instances and collections, and the rest waits for a free worker (freed workers are split evenly among the collections being ingested).\n'
        'Changes take effect from the next ingestion started (no need to restart the admin tool).'
    ),
    'vector_db.ingest_processes': (
        'Number of child processes that extract text and render pages as images (0 or more). 0 (default) uses no child processes, doing it in the ingest_workers threads.\n'
        'Extraction and rendering use the CPU, and Python threads only make progress on one at a time (GIL), so if there are many PDFs, Excel files, etc. and CPU\n'
        'cores to spare, setting it to 2 or more, using the number of cores as a guide, makes it faster (PDF processing (pypdfium2) is also parallelized per process).\n'
        'Starting child processes takes a few seconds and each process uses memory, so 0 is fine when there are few files.\n'
        'When a child process crashes (PDFium fell over on a broken PDF, etc.), only that file is listed in the failure list and ingestion continues\n'
        '(with 0 the whole admin tool goes down, so for sources with many files of unknown origin, 1 or more is safer).\n'
        'With 1 or more, this is also the number of files prepared at the same time (in total across all instances and collections).'
    ),
    'vector_db.ingest_embed_requests': (
        'Number of embedding requests sent at the same time during ingestion (0 or more). 0 (default) means the same as ingest_workers.\n'
        'A file is embedded in several requests (text chunks in batches of embedding_batch_size, page images one at a time), so batches of a large file are also sent at the same time.\n'
        'Embedding wait time only gets shorter if the inference server of embedding_url can process several requests at the same time\n'
        '(for llama-server, start it with --parallel (-np) at or above this value; otherwise requests just queue up and it does not get faster). Shared by all instances:\n'
        'at most this many are sent in total across the ingestions of all instances and collections\n'
        '(can be changed in "Admin settings" of the settings dashboard; cannot be changed per instance).'
    ),
    'vector_db.scan_workers': (
        'Number of folders read at the same time when scanning the source folder (threads, 1 or more). Each differential ingestion reads all folders of the source\n'
        'to find changed files. Folders on file servers (network shares) have a large round-trip\n'
        'latency per folder read, so reading in parallel makes it faster (the effect is small on local disks).\n'
        'Shared by all instances; change it on the "Admin settings" screen of the settings dashboard (it cannot be changed in config_overrides.json per instance).'
    ),
    'vector_db.max_files_per_collection': (
        'Maximum number of files ingested per collection (1 or more). If the source has more files than this, only files up to the limit are ingested,\n'
        'and a warning is shown on the Knowledge screen (already ingested files take priority, and the remaining slots are taken from the start of the new files in path order,\n'
        'so the selected files do not change between ingestions. Files not ingested because of the limit are not treated as deleted as long as they are in the folder).\n'
        'Ingestion keeps the target files and the records of ingested files in memory (a few GB for 1 million), so this is a limit to keep the admin tool\n'
        'from using too much memory. For large document sets, split collections per source folder.\n'
        'Shared by all instances; change it on the "Admin settings" screen of the settings dashboard (it cannot be changed in config_overrides.json per instance).'
    ),
    'vector_db.top_k': (
        'Number of results search_documents returns by default.'
    ),
    'vector_db.max_result_chars': (
        "Maximum number of characters of the whole search_documents result. Chunk text beyond it is truncated (to keep the main agent's token usage down)."
    ),
    'vector_db.min_score': (
        'Lower limit of the similarity (0.0 to 1.0, cosine similarity) of text chunks shown in results. Chunks below this are not returned.'
    ),
    'vector_db.image_min_score': (
        'Lower limit of the similarity of page images embedded with image_input = true. The similarity between a search term (text) and an image comes out lower than for text,\n'
        'so it is specified separately from min_score (adjust it by looking at what values unrelated pages get with the embedding model you use).'
    ),
    'vector_db.path_min_score': (
        'Lower limit of the similarity (embedding) of file paths shown in the results of search_document_paths (search by file and folder names).\n'
        'Ingestion embeds the path of each file relative to the source folder (e.g. ProductA/exterior_photos/weld_NG_example.jpg), one per file,\n'
        'and puts it in a separate collection (<name>.path). Paths are short strings, so if the values come out differently from the text, adjust it separately from min_score.'
    ),
    'vector_db.rrf_weight_text': (
        'Search merges three results by rank, not by score: text embeddings (rrf_weight_text), text keywords (rrf_weight_keyword; BM25 of SQLite FTS5, strong at exact matches\n'
        'of model numbers, part numbers and numbers) and page images (rrf_weight_image; when image_input = true)\n'
        '(Reciprocal Rank Fusion: sorted by the sum of weight / (60 + r) for the rank r in each result). Chunks of the same file and page are merged into one.\n'
        "Raising a weight makes that search's results more likely to rank high. 0 disables that search.\n"
        'Weight of the results of search by text embeddings (closeness of meaning). Strong with paraphrases and spelling variations. Chunks below min_score are removed before merging.'
    ),
    'vector_db.rrf_weight_keyword': (
        'Weight of the results of text keyword search (BM25 of SQLite FTS5). Strong at finding words that match literally, such as model numbers, part numbers, numbers and proper nouns.\n'
        'Ranks are merged with the same formula as the embedding results (weight / (60 + rank)). 0 disables keyword search (semantic search only).'
    ),
    'vector_db.rrf_weight_image': (
        'Weight of the results of search by page image embeddings (pages/sheets rendered one by one as images and ingested with image_input = true).\n'
        'Picks up content that cannot be extracted as text, such as figures, tables and scanned PDFs. Pages below image_min_score are removed before merging.\n'
        'Ranks are merged with the same formula as the embeddings (weight / (60 + rank)). 0 disables image search (collections without ingested images just give no results).'
    ),
    'vector_db.rrf_weight_path_keyword': (
        'In search_document_paths, the weight used when merging the results of file path keyword search (BM25 of SQLite FTS5; strong at exact matches of model numbers, part numbers, dates, etc.)\n'
        'with the results of file path embeddings (weight 1.0, those below path_min_score removed), using the same formula (weight / (60 + rank)).\n'
        '0 disables file path keyword search (semantic search only).'
    ),
    'log.dir': (
        'Output directory of the app logs.'
    ),
    'log.level': (
        'Log verbosity. One of these 3 values:\n'
        'info  - the current behavior. Records only a summary of each tool call (tool name, summarized arguments).\n'
        "debug - in addition to info, records all arguments and results of tool calls, and the LLM's response text and thinking (reasoning_content) (the amount of information and file size grow a lot; use it when you want to follow the model's behavior faithfully when investigating problems).\n"
        '* Nothing is output to the console (file_handler only).\n'
        '* DEBUG logs from openai/httpx/httpcore/aiosqlite/asyncio are suppressed.\n'
        'none  - no log output at all (nothing is created, including data/logs/app_*.log).'
    ),
    'log.clear_on_startup': (
        'Whether to create a new app_*.log file on every startup, or try to append to the most recent existing file (true/false).\n'
        'false (default): try to append to the most recent app_*.log (if it already exceeds max_lines, it rotates to a new file on the spot).\n'
        'true          : always create a new file with the date and time on every startup.\n'
        'With level=none no log is written at all, so this flag has no meaning.'
    ),
    'log.max_lines': (
        'Maximum number of lines per app_*.log file (the app log written to dir above). When exceeded, it rotates to a new file name with the date and time (e.g. app_20260725_143012.log).\n'
        'The date and time in the file name are set to "now" on every rotation (not fixed at the startup time). When rotating several times within the same second, sequence numbers _1, _2... are added to avoid collisions.\n'
        'When one record (an exception traceback, etc.) spans several lines, all its lines are counted, so the threshold refers to "the actual number of lines in the file". With level=none app_*.log itself is not created, so it has no meaning.'
    ),
    'log.retention_days': (
        'Retention in days of the old app_*.log files that keep growing with rotation. Files older than this (by modification time) are deleted automatically. 0 or less disables automatic deletion. evals.log, which evals/run_case.py writes in the same dir, is handled separately and is not deleted.'
    ),
    'log.cleanup_interval_hours': (
        'Interval (hours) of the automatic deletion check. The check also runs once at startup.'
    ),
    'chat_log.enabled': (
        "Enables/disables recording conversation logs (user messages and the AI's final responses) in text files.\n"
        'false (default): not recorded.'
    ),
    'chat_log.dir': (
        'Root directory where conversation logs are saved. Written in the structure <dir>/<username>/<date>_<thread_id>.log. The username is the identifier of the logged-in user (grouped as "anonymous" when [auth] enabled=false).'
    ),
    'thread_store.enabled': (
        'Enables/disables the conversation thread list (left sidebar of the screen) and choosing and resuming past conversations.\n'
        'true (default): enabled. false hides the sidebar itself and goes back to the single-session behavior from before this feature was added.'
    ),
    'thread_store.db': (
        "SQLite file where the thread list and step history are saved. A separate file from data/checkpoints.sqlite (LangGraph's conversation state itself). A lightweight store holding only the metadata needed for listing and resuming."
        ' Local disk only (UNC paths and network drives are rejected at startup and when saving on the dashboard, because SQLite fails with "database is locked" on a network share). Back up to a file share with [data_backup] instead.'
    ),
    'thread_store.retention_days': (
        'Retention in days of the thread list. Threads not updated for this many days are deleted automatically.\n'
        "0 or less (default) disables it = kept indefinitely. Deleting does not remove LangGraph's conversation state (checkpoints.sqlite) itself (they just no longer appear in the list)."
    ),
    'thread_store.cleanup_interval_hours': (
        'Interval (hours) of the automatic deletion check above.'
    ),
    'chat_starters.prompts': (
        'Canned prompt buttons shown when a chat starts. Clicking one sends it as a message as is and starts the task. Specify them in the same JSON/Python-style list form as [thinking_loop_guard].nudge_messages (square brackets, multiple lines OK, trailing comma allowed). An empty list [] shows no buttons.'
    ),
    'chat_starters.prompts_en': (
        'Canned prompts used for the English display (sessions where English was chosen with [ui].default_language or the language switch on the screen). The format is the same as prompts. An empty list [] or empty value shows prompts as is.'
    ),
    'user_response_timeouts.approval_seconds': (
        "Timeout in seconds when waiting for a user response (Chainlit's cl.AskActionMessage/cl.AskUserMessage). When there is no response, it errs on the safe side (approvals time out, questions are treated as errors). Different from [scripts].timeout (the execution timeout of scripts/code themselves).\n"
        'Waiting time for approval (approve_plan) of a plan made with create_plan.\n'
        '0 waits indefinitely.'
    ),
    'user_response_timeouts.ask_user_question_seconds': (
        'Waiting time for AskUserQuestion (free-text questions; a single question when labels is omitted, a multi-item form when labels is given). 0 waits indefinitely.'
    ),
    'user_response_timeouts.ask_user_choice_seconds': (
        'Waiting time for ask_user_choice (multiple-choice questions). 0 waits indefinitely.'
    ),
    'plan.allow_badge_unlock': (
        'Behavior when clicking the Plan Mode / Edit Automatically badge shown near the send button.\n'
        'true : can switch both ways (including Plan Mode -> Edit Automatically).\n'
        'The lock on write tools can be released with one click, without going through the user approval process of approve_plan.\n'
        'false: only one way, Edit Automatically -> Plan Mode, is allowed (only clicks in the locking direction work). Releasing Plan Mode -> Edit Automatically cannot be done by clicking the badge and always has to go through approve_plan (the user approval flow via the LLM). Change it to false as a safety valve if switching both ways causes problems.'
    ),
    'plan.auto_approve': (
        'Whether to approve approve_plan automatically without asking the user.\n'
        'true : when approve_plan is called, no approve/reject buttons are shown and no response is awaited; it is treated as approved on the spot. From then on, the hard block on run_script/run_script_background/execute_python_code/execute_python_code_background is released without human confirmation.\n'
        'false (default): as before, show the approve/reject buttons and require an explicit response from the user.'
    ),
    'plan.reset_approval_on_recreate': (
        'Behavior when create_plan is called while already in the Edit Automatically (plan approved) state.\n'
        'true : always force it back to Plan Mode. Even with an approved plan, plan_approved is reset unconditionally every time create_plan is called again, and re-approval with approve_plan is required.\n'
        'false: if already approved, keep Edit Automatically as is and only replace the steps (write tools can continue without calling approve_plan again). When called from the unapproved state, it stays in Plan Mode as before regardless of true/false (approve_plan is required). Change it to false if create_plan is often called again in the middle of an approved plan and re-approving every time feels excessive.'
    ),
    'plan.reset_approval_on_new_message': (
        'Whether to reset plan_approved unconditionally to Plan Mode (unapproved) every time a new user message is received.\n'
        'true : always reset (default, the old behavior). A safety measure that treats plan approval as scoped to "executing the plan made in response to that user message", so that abandoned old plans are not carried over to the next message.\n'
        'false: keep the approved state across messages. Even when thinking_loop_guard reaches its retry limit and is cut off, and the user sends a new message such as "continue" to keep working on the same task, the approved plan does not have to be redone from create_plan/approve_plan. Note that the previous approval also remains when an unrelated new request is sent.'
    ),
    'plan.plan_approval_exempt_scripts': (
        'A whitelist of read-only scripts exempted from plan approval (Plan Mode) for run_script/run_script_background. Specify it as a Python/JSON-style list [["skill name","script file name"], ...] (trailing comma allowed). If empty, the 7 default entries below are used.\n'
        'Limit it to "read-only scripts without side effects" (be careful: adding write scripts would let writes run without plan approval).\n'
        'A value saved on the settings dashboard (the per-instance config_overrides.json) does not replace the defaults; only entries not in the defaults are added (default entries cannot be removed, so that new skills added to the defaults by an update are not wiped out by a previously saved value).'
    ),
    'auth.enabled': (
        'Turns login authentication on/off.\n'
        'false (default): no login needed (as now, anyone can use it right away).\n'
        'true          : a login screen is shown at startup, and logging in with a username/password registered in AUTH_USERS of .env is required.\n'
        'The actual usernames and passwords, and the required CHAINLIT_AUTH_SECRET, are managed in .env (environment variables) (by policy, secrets are not put in config.ini, so only the on/off flag is here; for details see .env.example / README_DETAIL.md).'
    ),
    'auth.require_password': (
        'Switches whether a password is required when enabled=true.\n'
        'true (default) : you cannot log in unless both the username and password match an entry in AUTH_USERS of .env exactly.\n'
        'false        : as long as the username is registered in AUTH_USERS, you can log in whatever password you enter (even empty). A relaxation switch for environments such as an internal LAN, where network reachability itself is closed enough and it is enough to tell users apart and record them by username.'
    ),
    'ui.default_language': (
        'Default display language of the screen. One of auto / ja / en.\n'
        'When a user chooses a language in the display settings at the top right of the screen (palette icon), it is saved in the browser and takes precedence from then on. This value is the initial value for "users who have not chosen yet".\n'
        "auto (default): follow the browser's language setting (English if English, Japanese otherwise).\n"
        'The AI also answers in the selected language (the prompts themselves are in English, and an instruction on the response language is added at the end; see src/i18n.py). The display language of the settings dashboard is decided by the same rule.'
    ),
    'ui.max_display_messages': (
        'Maximum number of messages (user messages / AI responses, per cl.Message step) drawn in the main thread of the chat screen (frontend).\n'
        'Old messages over the limit are only dropped from the display; it has no effect at all on the conversation context passed to the LLM or on the conversation logs ([chat_log]) (display-only thinning; see frontend/src/App.tsx).\n'
        '0 means unlimited (draw everything as before).'
    ),
    'ui.max_display_side_steps': (
        'Maximum number of steps drawn in the side panel (on the right, the list of steps such as tool calls).\n'
        'Old steps over the limit are only dropped from the display; it has no effect at all on the conversation context passed to the LLM or on the conversation logs (display-only thinning).\n'
        '0 means unlimited (draw everything as before).'
    ),
    'ui.paste_as_attachment_threshold_chars': (
        'When text of this many characters or more is pasted into the input box, it is not expanded in the input box but handled as a "pasted text" card (an attachment pasted-text-<time>.txt).\n'
        'A countermeasure for the browser freezing when huge text is poured directly into the textarea.\n'
        'When sent, it is passed to the LLM as text, not as a file path (see _build_human_message in app.py). 0 or less disables it (the whole text is expanded in the input box as before).'
    ),
    'ui.max_input_chars': (
        'Maximum number of characters accepted in one send (the text of the input box + pasted text in total).\n'
        'While over the limit, the send button is disabled, and the backend also rejects sends over the limit.\n'
        "Japanese is roughly 1 character ≈ 1 token (English is roughly 4 characters per token), so keep it within the context length of the LLM server (llama-server's --ctx-size) minus what the system prompt, tool definitions and answer use. 0 or less means unlimited."
    ),
    'ui.token_usage_warn_threshold': (
        'In the "Token usage" card of the side panel, when the total tokens (total) of the "per request" row (the most recent LLM call; not the cumulative rows) reaches this value or more, the whole row is highlighted in bold orange. It is only a display warning and does not affect actual generation limits such as [llm].max_tokens.\n'
        '0 or less disables it.'
    ),
    'ui.token_usage_alert_threshold': (
        'When the same target as above (the total tokens of the "per request" row) reaches this value or more, the whole row is highlighted in bold red (takes precedence over token_usage_warn_threshold; 0 or less disables it).'
    ),
    'websocket.ping_interval_seconds': (
        'Liveness check interval and timeout of the WebSocket (Socket.IO) connection between the browser and the server.\n'
        'A setting on a different layer from [llm].stream_chunk_timeout_seconds (the communication timeout with the LLM server). Chainlit itself (`socketio.AsyncServer(...)` in chainlit/server.py) does not specify these, so when unset the python-socketio/engineio defaults (25-second interval, 20-second timeout) are used.\n'
        'If the 20-second ping_timeout is exceeded during periods when the event loop tends to block while waiting for LLM responses (such as during long dispatch_agent runs), the WebSocket can be disconnected even though backend processing continues, and step updates are not reflected in the UI timeline (side panel) until it reconnects.\n'
        'How many seconds apart the server sends pings.'
    ),
    'websocket.ping_timeout_seconds': (
        'If no response (pong) to the latest ping is received after waiting this many seconds, the connection is considered disconnected.\n'
        'Larger values are more tolerant of brief disconnections and response delays, but detection is slower when the connection is really dead (messages cannot be delivered during that time).'
    ),
    'mcp.enabled': (
        'Enables/disables the automatic connection to all MCP servers defined in mcpServers of .locohane/settings.json itself. If false, nothing is connected regardless of whether the file exists (if .locohane/settings.json has "mcp"."enabled", that takes precedence; this is the default when settings.json does not specify it).'
    ),
    'mcp.settings_path': (
        'Path of .locohane/settings.json (relative paths are relative to the project root).\n'
        'An MCP server connection settings file for the stdio transport only, modeled on the mcpServers settings format of Claude Code / Qwen Code (see src/mcp_client.py).'
    ),
    'mcp.connect_timeout_seconds': (
        'Timeout in seconds for starting one server (process start + initialize + tools/list). Servers that exceed it are treated as connection failures and skipped (the app keeps starting).'
    ),
    'mcp.call_timeout_seconds': (
        'Timeout in seconds per MCP tool call (tools/call).'
    ),
    'admin.host': (
        'Settings of the settings dashboard itself (admin tool, the admin/ package, admin.bat).\n'
        'It runs as a separate process on a separate port from Locohane itself (app.py). These values are not subject to override by instances/<name>/config_overrides.json (the override diffs of settings changed per instance in the admin tool; for details see "Settings dashboard" in README_DETAIL.md) (they are the admin tool\'s own startup settings, so this avoids circularity).\n'
        'They must be in effect before the admin tool starts, so they can only be overridden with environment variables such as ADMIN_HOST (not with config_overrides.json).\n'
        '\n'
        'Host the admin tool listens on. The default is local only. 0.0.0.0 makes it reachable from other devices on the LAN, but unless you set ssl_certfile/ssl_keyfile below to enable HTTPS, login passwords flow over the network in plain text. Do not change it unless you enable HTTPS, or the risk of eavesdropping on the route is acceptable, e.g. on an internal LAN.'
    ),
    'admin.port': (
        'Port the admin tool listens on.'
    ),
    'admin.ssl_certfile': (
        "Paths of the server certificate (PEM) and private key (PEM) for HTTPS. When both are given, the admin tool and the main instances started from the admin tool or app.bat (chainlit's --ssl-cert/--ssl-key) listen over HTTPS with the same certificate. If both are empty, HTTP (default). Specifying only one, or a missing file, makes startup refuse. Relative paths are relative to the project root. Can be overridden with the environment variables SSL_CERTFILE / SSL_KEYFILE."
    ),
    'admin.ssl_keyfile': (
        'Path of the private key (PEM) for HTTPS. HTTPS is used only when both this and ssl_certfile are given (specifying only one, or a missing file, makes startup refuse).\n'
        'Relative paths are relative to the project root. Can be overridden with the environment variable SSL_KEYFILE.'
    ),
    'admin.session_timeout_minutes': (
        'Lifetime (minutes) of login sessions to the admin tool.'
    ),
    'admin.backup_keep': (
        'How many generations of backups of config.ini/public/settings/.env (instances/<name>/backups/) to keep per instance. The excess is deleted oldest first.'
    ),
    'admin.stop_apps_on_exit': (
        'Whether to also stop the main instances (child processes) started from the admin tool when the admin tool exits. If false (default), the main instances keep running when the admin tool exits (when the admin tool restarts, they are detected as "running externally").'
    ),
    'admin.instances_dir': (
        'Root of the per-instance directories (instance.json, config_overrides.json, .env, settings/, backups/, app_stdout.log, and admin_changes.log). Relative paths are relative to the project root. Used not only by the admin tool but also as the location from which the .env and config_overrides.json of the default instance are read when started directly with app.bat. Can be overridden with the environment variable INSTANCES_DIR. If you change it, move the contents of the existing instances/ by hand (there is no automatic migration).'
        ' Local disk only (UNC paths and network drives are rejected at startup and when saving on the dashboard, because SQLite fails with "database is locked" on a network share). Back up to a file share with [data_backup] instead.'
    ),
    'admin.assistant_enabled': (
        'Whether to enable "Ask AI" at the top right of the dashboard. For an administrator\'s problem, the AI advises which screen and which config keys to change (the AI only reads settings and never changes, saves, starts or stops anything; people do the operations). false hides the button, and the API also returns 404. Can be overridden with the environment variable ADMIN_ASSISTANT_ENABLED.'
    ),
    'admin.assistant_llm_instance': (
        "Which instance's [llm] settings to take the LLM endpoints used for answers from (instance name). If empty, the [llm] settings of the instance selected as the subject of the question are used. If you want to be able to ask even when the subject's LLM is down, or do not want to make production users wait, specify another instance name, such as a skill tuning worker. Can be changed in \"Admin settings\" of the settings dashboard (the value is saved in <instances_dir>/admin_settings.json and takes precedence over this; it cannot be changed per instance). Can be overridden with the environment variable ADMIN_ASSISTANT_LLM_INSTANCE."
    ),
    'admin.assistant_max_keys': (
        'Maximum number of config.ini keys close to the question passed to the AI with their descriptions, defaults and current values (1 or more). More means fewer misses, but the input gets longer and answers slower (about 8-15 is recommended for fairly small local models). Can be overridden with the environment variable ADMIN_ASSISTANT_MAX_KEYS.'
    ),
    'admin.assistant_max_docs': (
        'Maximum number of paragraphs (up to about 1200 characters) of README_DETAIL.md and the API reference close to the question passed to the AI (0 or more; 0 passes none). Can be overridden with the environment variable ADMIN_ASSISTANT_MAX_DOCS.'
    ),
    'admin.assistant_use_embedding': (
        'Whether to use the same embedding server as Knowledge ([vector_db] embedding_url) to choose the keys and paragraphs close to the question. If true (default), the embedding_url of the instance in assistant_embedding_instance below is used (it is used if embedding_url is set even if [vector_db] enabled is false), and when none is found or it cannot connect, choices are made only by character overlap (in pairs of characters). If false, choices are always made only by character overlap. Embeddings are better at questions worded differently (e.g. "file size" vs max_file_mb). Can be overridden with the environment variable ADMIN_ASSISTANT_USE_EMBEDDING.'
    ),
    'admin.assistant_embedding_instance': (
        "Which instance's [vector_db] embedding_url to take the embedding endpoints from (instance name). If empty, the first instance with embedding_url set is used, in the order: the instance of the LLM used for answers -> the instance being asked about -> other instances (in name order). Can be changed in \"Admin settings\" of the settings dashboard (the value is saved in <instances_dir>/admin_settings.json and takes precedence over this; it cannot be changed per instance). Can be overridden with the environment variable ADMIN_ASSISTANT_EMBEDDING_INSTANCE."
    ),
    'admin.assistant_max_history_messages': (
        "Maximum number of recent conversation messages passed to the AI (the asker's messages and the AI's answers each count as one; 1 or more). The oldest are dropped first. The conversation history exists only in the browser and is not saved on the server. Can be overridden with the environment variable ADMIN_ASSISTANT_MAX_HISTORY_MESSAGES."
    ),
    'data_backup.enabled': (
        'Backs up [admin] instances_dir and the [paths] common_data_dir of all instances into a dated zip in dest_dir (runs only while the admin tool is running).\n'
        'Shared by all instances (cannot be overridden per instance). Values saved on the dashboard are kept in <instances_dir>/data_backup.json and take precedence over the values here (these are the defaults).\n'
        'SQLite databases (conversation history, thread list, knowledge, etc.) are copied with the SQLite backup feature so they stay consistent even while in use. Qdrant data (on the Qdrant server) is not included.\n'
        'Whether to run the scheduled backup. Even if false, "Back up now" on the screen can be used.'
    ),
    'data_backup.dest_dir': (
        'Destination folder (absolute path). A file share (UNC path \\\\server\\share\\...) can also be specified (the intended use is to keep the data on a local disk and save it here).\n'
        'It cannot be inside the backup sources (instances_dir or any common_data_dir). If empty, no backup is made.'
    ),
    'data_backup.times': (
        'Times of the scheduled backup (24-hour HH:MM; separate several with commas, e.g. 02:00, 14:00).'
    ),
    'data_backup.weekdays': (
        'Days of the scheduled backup (0=Mon to 6=Sun, comma separated). Empty means every day.'
    ),
    'data_backup.keep': (
        'Number of generations to keep (1 or more). The excess zips are deleted oldest first.'
    ),
    'data_backup.exclude_workdir': (
        "Do not back up the working folder ([default_workdir] dir; it tends to grow with users' working files)."
    ),
    'data_backup.exclude_logs': (
        "Do not back up logs ([log] dir, [chat_log] dir, the admin tool's log instances/_admin_logs, app_stdout.log)."
    ),
    'data_backup.exclude': (
        'Additional exclusion patterns (globs matched against the path relative to the backup source folder; comma separated, e.g. */skill_drafts_archive/*, *.tmp).'
    ),
}

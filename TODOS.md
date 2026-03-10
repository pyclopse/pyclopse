# TODOs / Upstream Contributions

## FastAgent: Add `delta.reasoning_details` support in `_process_stream_chunk_common`

**File**: `fast_agent/llm/provider/openai/llm_openai.py`
**Method**: `OpenAILLM._process_stream_chunk_common`

### Problem

FastAgent's stream chunk processor calls `_extract_reasoning_text` with only
`delta.reasoning` and `delta.reasoning_content`, ignoring `delta.reasoning_details`:

```python
reasoning_text = self._extract_reasoning_text(
    reasoning=getattr(delta, "reasoning", None),
    reasoning_content=getattr(delta, "reasoning_content", None),
    # ← delta.reasoning_details is never read
)
```

MiniMax M2.5 (and potentially other providers) use `delta.reasoning_details`
when `reasoning_split=True` is passed via `extra_body`.  This field is a
**cumulative** list of dicts: `[{"text": "<all thinking so far>"}]`.

Without this fix, thinking content is silently dropped and
`StreamChunk(is_reasoning=True)` is never emitted for MiniMax.

### Proposed Fix

In `_process_stream_chunk_common`, after the existing `_extract_reasoning_text`
call, also check `delta.reasoning_details` and emit incremental reasoning chunks:

```python
# After existing reasoning_text handling:
reasoning_details = getattr(delta, "reasoning_details", None)
if reasoning_details:
    for detail in reasoning_details:
        if isinstance(detail, dict) and "text" in detail:
            # Field is cumulative — diff against per-instance buffer
            buf = getattr(self, "_reasoning_details_buf", "")
            new_text = detail["text"]
            if not new_text.startswith(buf):
                buf = ""  # new stream session detected
            incremental = new_text[len(buf):]
            self._reasoning_details_buf = new_text
            if incremental:
                self._notify_stream_listeners(
                    StreamChunk(text=incremental, is_reasoning=True)
                )
```

Alternatively, `_extract_reasoning_text` could be extended to accept
`reasoning_details` and handle the cumulative diffing internally (though that
requires the instance buffer to be passed in or stored differently).

### Context

- MiniMax M2.5 API docs: pass `extra_body={"reasoning_split": True}` to
  separate thinking from response in streaming.
- Without `reasoning_split`, thinking leaks into `delta.content` interleaved
  with response tokens — streaming output is garbled.
- With `reasoning_split`, `delta.content` is clean but thinking is only in
  `delta.reasoning_details` which FastAgent ignores.

### Workaround (currently in pyclaw)

`pyclaw/agents/runner.py` monkey-patches `OpenAILLM._process_stream_chunk_common`
at class level the first time a `generic.*` model runner initialises.  This
workaround can be removed once FastAgent ships the fix upstream.

See: `_patch_openai_llm_for_reasoning_details()` in `pyclaw/agents/runner.py`

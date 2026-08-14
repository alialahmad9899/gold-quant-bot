# Gemini Model Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (required) to implement this plan task-by-task.

**Goal:** Stop invalid Gemini model fallbacks from wasting quota and make Dynamic Discovery select models compatible with the existing `models.generate_content()` path.

**Architecture:** Keep the current Gemini client and dynamic discovery, but add a capability gate before a model enters the routing pool. Explicitly exclude Live/Image/Audio/Robotics/Deep-Research/Computer-Use model families from the text generation path. Treat 404 and Interactions-only 400 errors as temporary incompatibility signals, while applying cooldowns to 429s and bounding the candidate list so fallback remains fast and quota-efficient.

**Tech Stack:** Python, `google-genai`, pytest, existing `bot.py` Gemini router.

## Global Constraints

- Do not modify Twelve Data WebSocket/runtime behavior.
- Keep `google-genai` as the Gemini SDK.
- Preserve Dynamic Discovery; do not hard-code a single permanent model.
- Do not silently convert Gemini failures into fake successful analysis.
- Keep fallback bounded and avoid retry storms.

---

### Task 1: Add failing tests for model capability filtering and bounded routing

**Files:**
- Test: `tests/test_gemini_model_routing.py`

- [ ] Write tests covering exclusion of Live/Image/Audio/Deep-Research/Computer-Use families, preference for text Flash/Pro candidates, 400 Interactions-only blacklisting, 404 blacklisting, 429 cooldown, and candidate-count bounding.
- [ ] Run the new tests and confirm they fail against the current router.

### Task 2: Implement capability-aware discovery

**Files:**
- Modify: `bot.py` in the Dynamic Discovery/priority helpers.
- Test: `tests/test_gemini_model_routing.py`

- [ ] Add explicit incompatible-name family filters.
- [ ] Use `supported_generation_methods` when present as a positive signal, never as the sole compatibility guarantee.
- [ ] Keep only models suitable for the existing `generate_content()` call path.
- [ ] Bound the returned candidate list.
- [ ] Run targeted tests and confirm green.

### Task 3: Harden fallback error handling

**Files:**
- Modify: `bot.py` in `execute_gemini_dynamic_request`.
- Test: `tests/test_gemini_model_routing.py`

- [ ] On 404 or Interactions-only 400, temporarily blacklist the model and continue to the next compatible candidate.
- [ ] On 429, apply cooldown and move on instead of immediately retrying the same model twice.
- [ ] Preserve existing behavior for other errors and final failure reporting.
- [ ] Run targeted tests and confirm green.

### Task 4: Full verification and integration

**Files:**
- Modify: only files from Tasks 1-3.

- [ ] Run `python -m py_compile bot.py twelve_data_gateway.py`.
- [ ] Run `python -m pytest -q`.
- [ ] Run `git diff --check`.
- [ ] Confirm Twelve Data WebSocket code is unchanged by the Gemini fix.
- [ ] Commit and open a PR to `main`.

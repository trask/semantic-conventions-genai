---
description: "Use when implementing or updating reference instrumentation, manual spans, reference tag/attribute emission, or data coverage in reference scenarios. Keeps reference instrumentation easy to scan and avoids helpers that hide emitted attributes."
applyTo: "reference/scenarios/**/scenario.py"
---

# Reference Instrumentation

- Set emitted attributes and tags inline at the instrumentation site.
- Do not move attribute emission into helper methods such as `setServerTags`, `setServerAttributes`, `_set_server_attributes`, or similar wrappers.
- Small local parsing or derivation that exists only to support nearby emitted attributes is fine, but keep it in the same span or activity block.
- If a method owns its own span boundary, set that span's attributes inline in that method.
- Keep reference instrumentation easy to scan: the base attributes, derived attributes, and result attributes should appear together.
- For attributes whose value is not truly static for the scenario, do not hardcode the emitted value. Use a local variable or field read that comes from the current request or current response.
- Request-side attributes such as `gen_ai.request.model` should come from the same variable or object field that is passed into the SDK call.
- Response-side attributes such as `gen_ai.response.model`, response ids, finish reasons, and token counts should come from the current response or streamed result object, optionally via a small nearby local variable.
- If you need the same non-static value in both the request and span attributes, bind it once locally and reuse that variable in both places.

// POST /functions/v1/report-savings
// JWT-verified. Accepts ONLY aggregate numeric counters and upsert-accumulates
// them into savings_daily for the calling user (via the report_savings RPC,
// which runs under the caller's RLS).
//
// Privacy contract: this endpoint accepts numbers plus a YYYY-MM-DD day key
// and NOTHING else. Any unknown field, any string/content payload, is
// rejected outright so prompt content can never leak into telemetry.

import { error, json, preflight } from "../_shared/cors.ts";
import { createUserClient, getUser } from "../_shared/supabase.ts";

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const COUNTER_FIELDS = ["tokens_before", "tokens_after", "tokens_saved", "requests"] as const;
const ALLOWED_KEYS = new Set<string>(["day", ...COUNTER_FIELDS, "usd_saved"]);
const MAX_COUNTER = Number.MAX_SAFE_INTEGER;

function isNonNegativeInt(v: unknown): v is number {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= 0 && v <= MAX_COUNTER;
}

Deno.serve(async (req) => {
  const gate = preflight(req);
  if (gate) return gate;

  const user = await getUser(req);
  if (!user) return error("Unauthorized", 401);

  let body: unknown;
  try {
    body = JSON.parse(await req.text());
  } catch {
    return error("Invalid JSON body", 400);
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return error("Body must be a JSON object", 400);
  }
  const payload = body as Record<string, unknown>;

  // Privacy guard: refuse any field we did not explicitly whitelist.
  const unknown = Object.keys(payload).filter((k) => !ALLOWED_KEYS.has(k));
  if (unknown.length > 0) {
    return error(`Unknown fields rejected (numbers only): ${unknown.join(", ")}`, 400);
  }

  if (
    typeof payload.day !== "string" || !DAY_RE.test(payload.day) ||
    Number.isNaN(Date.parse(payload.day))
  ) {
    return error("day must be a valid YYYY-MM-DD date string", 400);
  }
  for (const field of COUNTER_FIELDS) {
    if (!isNonNegativeInt(payload[field])) {
      return error(`${field} must be a non-negative integer`, 400);
    }
  }
  // `requests` maps to a Postgres int4 (report_savings p_requests int).
  if ((payload.requests as number) > 2_147_483_647) {
    return error("requests must fit in a 32-bit integer", 400);
  }
  // Explicit `"usd_saved": null` must be rejected, not coerced to 0.
  const usdSaved = "usd_saved" in payload ? payload.usd_saved : 0;
  if (typeof usdSaved !== "number" || !Number.isFinite(usdSaved) || usdSaved < 0) {
    return error("usd_saved must be a non-negative number", 400);
  }

  // User-scoped client: report_savings is SECURITY INVOKER, so RLS applies
  // and auth.uid() inside the function is this caller.
  const client = createUserClient(req);
  const { error: rpcErr } = await client.rpc("report_savings", {
    p_day: payload.day,
    p_tokens_before: payload.tokens_before,
    p_tokens_after: payload.tokens_after,
    p_tokens_saved: payload.tokens_saved,
    p_requests: payload.requests,
    p_usd_saved: usdSaved,
  });
  if (rpcErr) {
    console.error("report_savings rpc failed", rpcErr);
    return error("Failed to record savings", 500);
  }

  return json({ ok: true });
});

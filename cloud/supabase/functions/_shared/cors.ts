// Shared CORS + JSON response helpers for Headroom Desktop edge functions.

export const corsHeaders: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type, stripe-signature",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

export function error(message: string, status: number): Response {
  return json({ error: message }, status);
}

/** Standard preflight / method gate. Returns a Response to short-circuit with, or null to continue. */
export function preflight(req: Request, method = "POST"): Response | null {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }
  if (req.method !== method) {
    return error(`Method not allowed; use ${method}`, 405);
  }
  return null;
}

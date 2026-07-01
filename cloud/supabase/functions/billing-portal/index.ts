// POST /functions/v1/billing-portal
// JWT-verified. Returns a Stripe Billing Portal session URL for the caller's
// existing Stripe customer.
//
// Secrets: STRIPE_SECRET_KEY. Optional: SITE_URL.

import { error, json, preflight } from "../_shared/cors.ts";
import { stripe } from "../_shared/stripe.ts";
import { createAdminClient, getUser } from "../_shared/supabase.ts";

const DEFAULT_SITE_URL = Deno.env.get("SITE_URL") ?? "https://headroom.local";

Deno.serve(async (req) => {
  const gate = preflight(req);
  if (gate) return gate;

  const user = await getUser(req);
  if (!user) return error("Unauthorized", 401);

  let body: Record<string, unknown> = {};
  try {
    const text = await req.text();
    if (text) body = JSON.parse(text);
  } catch {
    return error("Invalid JSON body", 400);
  }
  const returnUrl = typeof body.return_url === "string"
    ? body.return_url
    : `${DEFAULT_SITE_URL}/billing`;

  const admin = createAdminClient();
  const { data: row, error: selectErr } = await admin
    .from("subscriptions")
    .select("stripe_customer_id")
    .eq("user_id", user.id)
    .maybeSingle();
  if (selectErr) {
    console.error("subscriptions lookup failed", selectErr);
    return error("Internal error", 500);
  }
  if (!row?.stripe_customer_id) {
    return error("No billing account for this user; start a checkout first", 404);
  }

  try {
    const session = await stripe.billingPortal.sessions.create({
      customer: row.stripe_customer_id,
      return_url: returnUrl,
    });
    return json({ url: session.url });
  } catch (err) {
    console.error("billing-portal failed", err);
    return error("Failed to create billing portal session", 500);
  }
});

// POST /functions/v1/create-checkout
// JWT-verified. Creates (or reuses) the Stripe customer for the caller,
// persists the mapping in `subscriptions` via the service role, then creates
// a subscription Checkout Session for STRIPE_PRICE_ID and returns its URL.
//
// Secrets: STRIPE_SECRET_KEY, STRIPE_PRICE_ID. Optional: SITE_URL.

import { error, json, preflight } from "../_shared/cors.ts";
import { stripe } from "../_shared/stripe.ts";
import { createAdminClient, getUser } from "../_shared/supabase.ts";

const DEFAULT_SITE_URL = Deno.env.get("SITE_URL") ?? "https://headroom.local";

Deno.serve(async (req) => {
  const gate = preflight(req);
  if (gate) return gate;

  const user = await getUser(req);
  if (!user) return error("Unauthorized", 401);

  const priceId = Deno.env.get("STRIPE_PRICE_ID");
  if (!priceId) return error("Billing is not configured (STRIPE_PRICE_ID missing)", 500);

  let body: Record<string, unknown> = {};
  try {
    const text = await req.text();
    if (text) body = JSON.parse(text);
  } catch {
    return error("Invalid JSON body", 400);
  }
  const successUrl = typeof body.success_url === "string"
    ? body.success_url
    : `${DEFAULT_SITE_URL}/billing/success`;
  const cancelUrl = typeof body.cancel_url === "string"
    ? body.cancel_url
    : `${DEFAULT_SITE_URL}/billing/cancelled`;

  const admin = createAdminClient();

  try {
    // Reuse an existing Stripe customer if we have one on file.
    const { data: existing, error: selectErr } = await admin
      .from("subscriptions")
      .select("stripe_customer_id")
      .eq("user_id", user.id)
      .maybeSingle();
    if (selectErr) {
      console.error("subscriptions lookup failed", selectErr);
      return error("Internal error", 500);
    }

    let customerId = existing?.stripe_customer_id as string | null | undefined;
    if (!customerId) {
      const customer = await stripe.customers.create({
        email: user.email ?? undefined,
        metadata: { supabase_user_id: user.id },
      });
      customerId = customer.id;

      const { error: upsertErr } = await admin
        .from("subscriptions")
        .upsert(
          {
            user_id: user.id,
            stripe_customer_id: customerId,
            updated_at: new Date().toISOString(),
          },
          { onConflict: "user_id" },
        );
      if (upsertErr) {
        console.error("subscriptions upsert failed", upsertErr);
        return error("Internal error", 500);
      }
    }

    const session = await stripe.checkout.sessions.create({
      mode: "subscription",
      customer: customerId,
      client_reference_id: user.id,
      line_items: [{ price: priceId, quantity: 1 }],
      subscription_data: { metadata: { supabase_user_id: user.id } },
      success_url: successUrl,
      cancel_url: cancelUrl,
    });

    return json({ url: session.url });
  } catch (err) {
    console.error("create-checkout failed", err);
    return error("Failed to create checkout session", 500);
  }
});

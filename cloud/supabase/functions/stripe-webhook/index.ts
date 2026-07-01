// POST /functions/v1/stripe-webhook
// Called by Stripe, NOT by clients. Authenticated by webhook signature, not
// a Supabase JWT -- deploy with `--no-verify-jwt` (and `verify_jwt = false`
// is set in config.toml).
//
// Handles: checkout.session.completed, customer.subscription.updated,
// customer.subscription.deleted. Upserts the `subscriptions` mirror via the
// service-role client. Returns 400 on bad signature.
//
// Secrets: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET.

import { cryptoProvider, stripe, type Stripe } from "../_shared/stripe.ts";
import { createAdminClient } from "../_shared/supabase.ts";

const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET") ?? "";

function periodEndIso(sub: Stripe.Subscription): string | null {
  // `current_period_end` lives on the subscription in classic API versions
  // and on the subscription item in 2025+ versions; handle both.
  const raw = (sub as unknown as { current_period_end?: number }).current_period_end ??
    (sub.items?.data?.[0] as unknown as { current_period_end?: number })
      ?.current_period_end;
  return typeof raw === "number" ? new Date(raw * 1000).toISOString() : null;
}

async function resolveUserId(
  sub: Stripe.Subscription,
  customerId: string,
): Promise<string | null> {
  // Preferred: metadata we stamp at checkout time.
  const fromMeta = sub.metadata?.supabase_user_id;
  if (fromMeta) return fromMeta;

  // Fallback: reverse lookup by customer id in our own mirror.
  const admin = createAdminClient();
  const { data } = await admin
    .from("subscriptions")
    .select("user_id")
    .eq("stripe_customer_id", customerId)
    .maybeSingle();
  if (data?.user_id) return data.user_id as string;

  // Last resort: customer metadata set in create-checkout.
  const customer = await stripe.customers.retrieve(customerId);
  if (!customer.deleted) {
    return (customer as Stripe.Customer).metadata?.supabase_user_id ?? null;
  }
  return null;
}

async function upsertSubscription(sub: Stripe.Subscription): Promise<void> {
  const customerId = typeof sub.customer === "string" ? sub.customer : sub.customer.id;
  const userId = await resolveUserId(sub, customerId);
  if (!userId) {
    // Not one of ours (e.g. shared Stripe account). Log and ack.
    console.warn(`No supabase user for stripe customer ${customerId}; skipping`);
    return;
  }

  const admin = createAdminClient();
  const { error } = await admin.from("subscriptions").upsert(
    {
      user_id: userId,
      stripe_customer_id: customerId,
      stripe_subscription_id: sub.id,
      status: sub.status,
      price_id: sub.items?.data?.[0]?.price?.id ?? null,
      current_period_end: periodEndIso(sub),
      updated_at: new Date().toISOString(),
    },
    { onConflict: "user_id" },
  );
  if (error) throw new Error(`subscriptions upsert failed: ${error.message}`);
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  const signature = req.headers.get("stripe-signature");
  if (!signature) {
    return new Response("Missing stripe-signature header", { status: 400 });
  }

  // Raw body is required for signature verification -- do not JSON.parse first.
  const body = await req.text();

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(
      body,
      signature,
      webhookSecret,
      undefined,
      cryptoProvider,
    );
  } catch (err) {
    console.error("Webhook signature verification failed", err);
    return new Response("Invalid signature", { status: 400 });
  }

  try {
    switch (event.type) {
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        if (session.mode === "subscription" && session.subscription) {
          const subId = typeof session.subscription === "string"
            ? session.subscription
            : session.subscription.id;
          const sub = await stripe.subscriptions.retrieve(subId);
          await upsertSubscription(sub);
        }
        break;
      }
      case "customer.subscription.updated":
      case "customer.subscription.deleted": {
        const sub = event.data.object as Stripe.Subscription;
        await upsertSubscription(sub);
        break;
      }
      default:
        // Acknowledge everything else so Stripe stops retrying.
        break;
    }
  } catch (err) {
    console.error(`Error handling ${event.type}`, err);
    return new Response("Handler error", { status: 500 });
  }

  return new Response(JSON.stringify({ received: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

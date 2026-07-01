// Shared Stripe client for Headroom Desktop edge functions.
// Requires secret: STRIPE_SECRET_KEY (set via `supabase secrets set`).

import Stripe from "npm:stripe@17";

const secretKey = Deno.env.get("STRIPE_SECRET_KEY");
if (!secretKey) {
  console.error("STRIPE_SECRET_KEY is not set");
}

export const stripe = new Stripe(secretKey ?? "", {
  // Fetch-based HTTP client: required in the Deno/edge runtime.
  httpClient: Stripe.createFetchHttpClient(),
  // apiVersion intentionally omitted -> uses the account's default version.
});

// SubtleCrypto provider for async webhook signature verification in Deno.
export const cryptoProvider = Stripe.createSubtleCryptoProvider();

export type { Stripe };

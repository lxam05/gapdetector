# Credits + Stripe Webhook – Testing Instructions

This doc describes how to test the **credits system** and **Stripe webhook** entitlement flow for Gap Detector.

## Prerequisites

- App running (e.g. `uvoron main:app --reload`).
- Postgres running; migrations applied: `alembic upgrade head`.
- `.env` includes:
  - `STRIPE_SECRET_KEY` (test key)
  - `STRIPE_WEBHOOK_SECRET` (from Stripe CLI or Dashboard)
  - `PRICE_SINGLE_REPORT` (1 report, €4.99)
  - `PRICE_BUNDLE_5` (5-report bundle, €19.99)
  - `PRICE_SUB_MONTHLY` (subscription, €24.99/month, 8 credits)

---

## 1) Run Stripe CLI webhook forwarder

Forward webhooks to your local server so `checkout.session.completed`, `invoice.paid`, and `customer.subscription.deleted` / `invoice.payment_failed` are received:

```bash
stripe listen --forward-to http://localhost:8000/stripe/webhook
```

Leave this running. Note the **webhook signing secret** (e.g. `whsec_...`) and set it in `.env` as `STRIPE_WEBHOOK_SECRET`, then restart the app so the webhook endpoint uses it to verify signatures.

---

## 2) Make a test purchase

1. **Single report (€4.99)**  
   - Create a scan (e.g. from the app home page).  
   - Click Unlock → Checkout. Pay with test card `4242 4242 4242 4242`.  
   - Stripe redirects to:  
     `.../report?paid=1&scan_id=<id>&session_id=cs_xxx`  
   - The frontend calls `POST /scan/<id>/unlock?session_id=cs_xxx`; the report unlocks.  
   - The webhook receives `checkout.session.completed` and grants **+1 credit** to the user identified by `customer_email` (or Stripe customer).

2. **Bundle of 5 (€19.99)**  
   - Use a Checkout Session created with `PRICE_BUNDLE_5` (if your UI supports product selection).  
   - After payment, webhook should grant **+5 credits**.

3. **Monthly subscription (€24.99/month)**  
   - Use a Checkout Session in `subscription` mode with `PRICE_SUB_MONTHLY`.  
   - After payment, webhook should set `subscription_active = true`, `monthly_quota = 8`, `credits_remaining = 8`, and `subscription_renewal_date`.

---

## 3) Verify credits increased in DB

After a successful payment, check the database:

- **Users:** `SELECT id, email FROM users WHERE email = '<customer_email>';`
- **Entitlements:**  
  `SELECT user_id, credits_remaining, monthly_quota, subscription_active, subscription_renewal_date FROM entitlements WHERE user_id = '<user_id>';`

For a single-report purchase you should see `credits_remaining` increased by 1 (or set to 1 if new). For the bundle, +5. For the subscription, `credits_remaining = 8`, `monthly_quota = 8`, `subscription_active = true`, and a renewal date set.

---

## 4) Generate report → credits decrease

1. Ensure the user has at least one credit (e.g. from step 2).
2. Create a new scan **as that user** (authenticated, or same email used in Checkout so entitlement is linked).
   - `POST /scan` with auth → backend checks entitlement; if `credits_remaining > 0`, decrements by 1, creates scan with `credits_used = true`, `user_id` set.
3. If the user has **no credits**, the same request should return **402 Payment Required** (or 403) with a message like "No credits remaining".
4. In the DB, confirm `entitlements.credits_remaining` decreased by 1 and the new scan has `credits_used = true` and `user_id` set.

---

## 5) Subscription renewal simulation

When a subscription renews, Stripe sends `invoice.paid` for the renewal invoice.

1. **Stripe CLI trigger (simulate renewal)**  
   ```bash
   stripe trigger invoice.paid
   ```  
   Use a subscription invoice in test mode, or create a subscription and wait for the renewal (or use Stripe’s “test clock” to advance time).

2. **Expected behavior**  
   - Webhook handler for `invoice.paid` finds the subscription’s price (`PRICE_SUB_MONTHLY`), finds the entitlement by `stripe_customer_id`, then:
     - Sets `credits_remaining = monthly_quota` (e.g. 8).
     - Updates `subscription_renewal_date` to the new period end.
   - No new user is created; same entitlement row is updated.

3. **Cancel / payment failed**  
   - Trigger `customer.subscription.deleted` or `invoice.payment_failed` (e.g. `stripe trigger customer.subscription.deleted` or similar).  
   - Handler should set `subscription_active = false`. Credits already on the account remain until used; no new credits are added until the subscription is reactivated or the user buys one-time credits.

---

## Quick checklist

| Step | Action | What to verify |
|------|--------|----------------|
| 1 | `stripe listen --forward-to http://localhost:8000/stripe/webhook` | CLI shows "Ready"; copy `whsec_...` into `STRIPE_WEBHOOK_SECRET`. |
| 2 | Pay for 1 report (or bundle/sub) | Redirect to report with `session_id`; report unlocks; webhook returns 200. |
| 3 | Query `entitlements` | `credits_remaining` (and for sub, `monthly_quota`, `subscription_active`, `subscription_renewal_date`) updated as expected. |
| 4 | Create scan with auth; then with 0 credits | First: scan created, credit decremented, `credits_used = true`. Second: 402/403 "No credits remaining". |
| 5 | Trigger `invoice.paid` for sub renewal | Entitlement: `credits_remaining` reset to `monthly_quota`, renewal date updated. |

---

## API reference (credits)

- **GET /me/credits** (authenticated)  
  Returns `{ credits_remaining, subscription_active, monthly_quota, renewal_date }` for the current user (from entitlement or zeros).

- **POST /stripe/webhook**  
  Stripe sends events here. Must verify signature with `STRIPE_WEBHOOK_SECRET`; return 200 for handled and irrelevant events so Stripe does not retry unnecessarily.

- **POST /scan**  
  Requires auth. Consumes 1 credit if `credits_remaining > 0`; returns 402/403 when no credits.

- **POST /scan/{id}/unlock?session_id=...**  
  Unlock by verified Stripe Checkout Session (`session_id`) or by authenticated user owning the scan. Do not trust `paid=1` alone.

# Paywall flow – local testing

This describes how to test the **report paywall** (teaser → Stripe → unlock) end-to-end locally.

## Prerequisites

- App running at `http://localhost:8000` (e.g. `uvicorn main:app --reload`).
- **Stripe configured for dynamic checkout** (recommended): set `STRIPE_SECRET_KEY` and `STRIPE_PRICE_ID` in `.env` so the app creates Checkout Sessions with a success URL that includes `scan_id`. No localStorage needed.
- Optional fallback: the static Payment Link (`STRIPE_PAYMENT_LINK_URL`) is used only when checkout is not configured or the checkout API fails.

## Success URL (must include scan_id)

The success URL after payment **must** include `scan_id` so we know which report to unlock:

```
http://localhost:8000/report?paid=1&scan_id={scan_id}
```

**How `{scan_id}` gets into the URL:**

1. **Dynamic Checkout Session (recommended)**  
   The app creates a Stripe Checkout Session server-side and sets:
   - `success_url = {SUCCESS_URL_BASE}/report?paid=1&scan_id={scan_id}`
   - So each checkout has the correct scan in the redirect.  
   **Requires:** `STRIPE_SECRET_KEY` and `STRIPE_PRICE_ID` in `.env`. The frontend calls `POST /scan/{scan_id}/checkout`, gets `{ url }`, and redirects the user to that URL. After payment, Stripe sends the user to the URL above with the real `scan_id`.

2. **Payment Link with URL parameters (if supported)**  
   Some setups let you pass parameters through the Payment Link so the success redirect includes them. Only use this if your Stripe product/docs confirm that the success URL can be parameterized per request. Otherwise, use dynamic Checkout Sessions above.

**Do not** rely on storing `scan_id` in localStorage and redirecting to a single `.../report?paid=1`; that breaks when the user pays in another tab, clears storage, or uses incognito.

## Stripe Dashboard setup

1. **If using dynamic Checkout Sessions**  
   You don’t set a success URL in the Dashboard for this flow; the app sets it per session. Ensure you have:
   - A **Price** for “1 report” (e.g. €4.99). Copy its **Price ID** (e.g. `price_xxx`) into `.env` as `STRIPE_PRICE_ID`.
   - Your **Secret key** (test) in `.env` as `STRIPE_SECRET_KEY`.

2. **If using the static Payment Link as fallback**  
   Edit the Payment Link → **After payment** → set:
   - **Success URL:** `http://localhost:8000/report?paid=1`  
   In that case the app can only recover `scan_id` from localStorage (same device/tab). Prefer dynamic checkout so the success URL is `...?paid=1&scan_id=<id>`.

For production, set `SUCCESS_URL_BASE` in `.env` to your domain (e.g. `https://yourdomain.com`). The app will build `success_url` from that base.

## Config (.env)

- `STRIPE_SECRET_KEY` – Stripe secret key (test or live). Required for dynamic checkout.
- `STRIPE_PRICE_ID` – Price ID for “1 report”. Required for dynamic checkout.
- `SUCCESS_URL_BASE` – Base for success URL (default: `http://localhost:8000`). Change for production.
- `STRIPE_PAYMENT_LINK_URL` – Static Payment Link URL; used only as fallback when checkout is not configured or fails.

## End-to-end test

1. **Create a scan**
   - Go to `http://localhost:8000` or `http://localhost:8000/app`.
   - Enter a company (e.g. "Notion") and submit.
   - You should be redirected to `/report?scan_id=<uuid>`.

2. **Locked report**
   - The report page shows the **teaser only** (summary, biggest weakness, a few complaints/opportunities, premium preview with lock overlay).
   - CTA: **Unlock Full Report — €4.99**.

3. **Go to Stripe**
   - Click **Unlock Full Report — €4.99**.
   - With dynamic checkout: the frontend calls `POST /scan/{scan_id}/checkout`, gets a Stripe Checkout URL, and redirects you there. (No localStorage.)
   - If checkout is not configured or returns an error: the frontend falls back to the static Payment Link and stores `scan_id` in localStorage.

4. **Pay (test mode)**
   - Use Stripe test card `4242 4242 4242 4242`, any future expiry, any CVC.
   - Complete checkout.

5. **Return and unlock**
   - Stripe redirects to:  
     `http://localhost:8000/report?paid=1&scan_id=<id>`  
     (with dynamic checkout; with static link you get `?paid=1` only and rely on localStorage fallback.)
   - The frontend calls `POST /scan/<id>/unlock?paid=1` (idempotent).
   - The frontend fetches the full report and renders it.
   - Refreshing `/report?scan_id=<id>` still shows the full report because unlock is stored server-side.

6. **Refresh**
   - Reload `/report?scan_id=<id>` (with or without `paid=1`). The scan is already unlocked server-side, so the full report stays visible.

## API summary (all under `/scan`)

- `POST /scan` – Create scan; returns `{ scan_id, ... }` (teaser). Frontend redirects to `/report?scan_id=...`.
- `GET /scan/{scan_id}/teaser` – Teaser only (always).
- `GET /scan/{scan_id}` – Full detail; includes `full_dashboard` when unlocked. Use this for both locked and unlocked states.
- `POST /scan/{scan_id}/checkout` – Create Stripe Checkout Session with `success_url` containing `scan_id`. Returns `{ url }`. Use this for the Unlock button when Stripe is configured.
- `POST /scan/{scan_id}/unlock?paid=1` – Mark scan unlocked. **Dev-only:** `?paid=1` allows unlock without auth. In production, replace with Stripe session verification and remove the `paid=1` bypass.

## Upgrading to Stripe session verification

Right now `paid=1` in the return URL is trusted (dev-only). To harden:

1. **Checkout Sessions** let you set `success_url` per request (with `scan_id`). After payment, you can redirect to e.g.  
   `https://yourdomain.com/report?session_id={CHECKOUT_SESSION_ID}&scan_id={scan_id}`  
   and verify the session with Stripe before unlocking.
2. **Payment Links** often don’t let you inject `{CHECKOUT_SESSION_ID}` or per-request parameters into the success URL the same way. If your Payment Link product doesn’t support that, use server-created Checkout Sessions instead.
3. Add a backend step that accepts `session_id` (and optionally `scan_id`), calls `stripe.checkout.Session.retrieve(session_id)`, checks payment status, then marks the scan unlocked. Remove or ignore the `paid=1` query param and rely only on session verification.

Unlock state is already stored in `Scan.is_unlocked` in the database; only the verification step needs to change.

## Troubleshooting: Stripe isn't redirecting after payment

**1. Are you using dynamic Checkout or the static Payment Link?**

- **Dynamic Checkout** (Unlock calls `POST /scan/{id}/checkout` and sends you to Stripe): The redirect is controlled by the `success_url` we set when creating the session. No Dashboard setting needed.
- **Static Payment Link** (Unlock sends you to the fixed `STRIPE_PAYMENT_LINK_URL`): The redirect is controlled **only** in the Stripe Dashboard. Go to [Payment Links](https://dashboard.stripe.com/test/payment-links) → your link → **After payment** / **Confirmation page** → set **Redirect to a webpage** and enter e.g. `http://localhost:8000/report?paid=1`. If this is set to "Show Stripe's page" or left default, Stripe will not redirect.

**2. Checkout Sessions: webhook can block redirect**

If you have a webhook endpoint for `checkout.session.completed`, Stripe only redirects the customer after the webhook returns **2xx**. If the webhook returns 4xx or 5xx, Stripe will **not** redirect. Fix the webhook so it returns 200 (or temporarily disable it) and try again.

**3. success_url must be reachable**

For test mode, `http://localhost:8000/...` is allowed. Ensure the app is running on that host/port and the path `/report` exists. If you use a tunnel (e.g. ngrok), set `SUCCESS_URL_BASE` to the tunnel URL so Stripe can redirect there.

**4. Verify dynamic checkout is used**

In the browser: when you click Unlock, open DevTools → Network and confirm a request to `POST /scan/.../checkout` returns 200 and a `url` to `checkout.stripe.com`. If you get 503, the app is using the fallback Payment Link; set `STRIPE_SECRET_KEY` and `STRIPE_PRICE_ID` in `.env` so dynamic checkout is used and the success URL includes `scan_id`.

**5. Restart the server after changing .env**

Settings are loaded once at startup. If you add or change `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, or `SUCCESS_URL_BASE` in `.env`, **restart the FastAPI server** (stop and start `uvicorn` again) so the new values are picked up. Otherwise checkout will keep returning 503 and you’ll be sent to the static Payment Link (no redirect back with scan_id).

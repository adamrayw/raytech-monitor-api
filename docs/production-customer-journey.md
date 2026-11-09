# Production customer journey checklist

Use a brand-new smoke-test account and record timestamps, transaction IDs, screenshots, and relevant request IDs. Use an approved provider test mode when available. If final validation requires a real QRIS payment, the owner must perform it manually. The scripts and application tests must never initiate or complete a monetary payment.

- [ ] 1. Open `https://monitor.raytech.cloud` and confirm HTTPS loads without certificate or console errors.
- [ ] 2. Register or sign in through Google OAuth and confirm the callback returns to Raytech Monitor without tokens in the URL.
- [ ] 3. Confirm the new account starts on Free with no paid transaction or Pro expiration.
- [ ] 4. Create up to three HTTP monitors at the 5-minute interval, one heartbeat, and one public status page.
- [ ] 5. Attempt to exceed each Free quota and confirm the API and UI reject the request without deleting existing resources.
- [ ] 6. Open Billing & Plan and confirm Free status, quota usage, Rp49.000 price, 30-day access, and manual renewal copy.
- [ ] 7. Create a QRISGate checkout and record the local transaction ID, external order ID, provider reference, amount, and expiration.
- [ ] 8. Before payment, confirm the transaction remains `PENDING`, `paid_at` is empty, and the account remains Free.
- [ ] 9. Complete an approved QRISGate test payment, or have the owner manually complete the real payment when a production smoke test is authorized.
- [ ] 10. Confirm QRISGate sends the signed callback and the backend performs an authenticated provider status lookup.
- [ ] 11. Confirm the local transaction changes from `PENDING` to `PAID` only after provider identity, amount, currency, and success status validation.
- [ ] 12. Confirm the account becomes Pro and the subscription audit log references the paid transaction.
- [ ] 13. Confirm `current_period_end` is exactly 30 days after activation for a new or expired subscription.
- [ ] 14. Confirm Pro quotas unlock: 20 HTTP monitors, 1-minute checks, 20 heartbeats, five status pages, SSL monitoring, and email incident alerts.
- [ ] 15. Confirm one payment confirmation email is queued and delivered to the customer, with billing support pointing to `support@raytech.cloud`.
- [ ] 16. Log out and confirm authenticated account and billing data are no longer accessible.
- [ ] 17. Log in again and confirm the Pro plan and expiration remain correct.
- [ ] 18. Confirm Billing History still shows the paid QRISGate transaction with the correct amount, reference, status, and paid time.
- [ ] 19. Replay the same signed callback or approved callback fixture and confirm the response is a successful no-op.
- [ ] 20. Confirm the replay did not add another 30 days, create another subscription audit event, or enqueue another confirmation email.

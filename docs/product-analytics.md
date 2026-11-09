# Product Analytics

Raytech Monitor stores first-party product events in PostgreSQL. The application does not use product analytics for authorization, billing settlement, quota enforcement, or monitor execution.

## Event model

`product_events` stores an authenticated user ID where available, event name, category, allowlisted properties, source, environment, idempotency key, and UTC creation time.

Client event properties are allowlisted. Page values are reduced to pathname only. Query strings and fragments are removed before storage.

The analytics system does not store passwords, OAuth tokens, API keys, payment credentials, sensitive headers, full request bodies, monitor URLs, or raw IP addresses.

## Taxonomy

Account events:

- `user_signed_up`
- `user_logged_in`
- `user_logged_out`

Onboarding events:

- `onboarding_started`
- `onboarding_completed`
- `first_monitor_created`
- `first_heartbeat_created`
- `first_status_page_created`

Monitoring events:

- `monitor_created`
- `monitor_deleted`
- `monitor_paused`
- `monitor_resumed`
- `heartbeat_created`
- `heartbeat_deleted`
- `status_page_created`
- `status_page_deleted`
- `ssl_monitor_enabled`
- `email_alerts_enabled`

Billing and subscription events:

- `pricing_viewed`
- `upgrade_clicked`
- `checkout_started`
- `payment_pending`
- `payment_succeeded`
- `payment_failed`
- `payment_expired`
- `pro_activated`
- `pro_renewed`
- `pro_expired`

Engagement and feedback events:

- `dashboard_viewed`
- `incident_viewed`
- `billing_viewed`
- `status_page_viewed`
- `feedback_opened`
- `feedback_submitted`

Important lifecycle events are written by the backend in the same database transaction as the related domain change. Client view events are accepted only for an allowlisted set and are deduplicated per user, event, pathname, and UTC date.

## Activation

Primary activation is `first_monitor_created` for the first successfully saved HTTP monitor.

`time_to_activation_seconds` is calculated from `users.created_at` to the first HTTP monitor creation time. Signup, heartbeat creation, status-page creation, and background monitor checks do not activate a user.

The admin activation rate uses:

```text
users with at least one HTTP monitor / total users
```

The dashboard also reports the tracked first-monitor event count through the funnel. Historical users may predate event collection, so the dashboard displays the analytics tracking start time.

## Funnel definitions

The tracked product funnel is sequential:

```text
Signup
First monitor
Pricing viewed
Upgrade clicked
Checkout started
Payment succeeded
Pro activated
```

A user counts in a stage only after counting in the prior stage, and the current event must occur at or after the prior event. Payment and Pro stages also require a production QRISGate transaction with `status = PAID`, `provider_mode = live`, and `revenue_classification = REAL`.

Each funnel row reports the stage count, prior-stage count, conversion percentage, and dropoff percentage. A percentage is not returned when its denominator is zero.

Additional conversion metrics use these denominators:

- Free to Pro: users with a genuine production paid transaction divided by total users.
- Pricing to checkout: Free users who started checkout divided by users who viewed pricing while their backend plan was Free.
- Checkout to payment: distinct users with an authoritative genuine production payment event divided by distinct users who started checkout.

## Retention

Retention cohorts use the UTC date of `user_signed_up`.

A user is retained for D1, D7, or D30 when the user records meaningful authenticated activity on or after the target cohort day. Meaningful events include login, dashboard, incident, billing, status-page views, and user-initiated resource interactions.

The calculation excludes background workers, monitor polling, payment polling, public status-page traffic, and heartbeat pings.

A retention metric returns `Insufficient data` until at least one signup cohort is old enough for the target day.

## Feature adoption

The admin API reports:

- Average HTTP monitors per user active in the last 30 days.
- Users with at least one HTTP monitor divided by total users.
- Users with at least one heartbeat divided by total users.
- Users with at least one status page divided by total users.
- Active Pro users with SSL monitoring divided by active Pro users.
- Active Pro users with email alerts divided by active Pro users.
- Distinct billing users and incident viewers.

## Feedback

Authenticated users can submit a category, message from 10 to 2000 characters, optional rating from 1 to 5, and sanitized page pathname. Submission is rate limited. Feedback is visible only through the server-protected admin API.

## Launch cohorts

Allowed launch cohorts are `internal`, `beta`, and `public`. New OAuth registrations are tagged `public`. Existing users remain unchanged until explicitly tagged.

Set a cohort for an existing exact email address with:

```bash
sudo docker exec raytech-monitor-api \
  python -m scripts.set_launch_cohort \
  --email USER_EMAIL \
  --cohort beta
```

The command locks the selected user row and does not create or promote accounts.

## Retention policy

Raw `product_events` should be retained for 180 days. Aggregated metrics may be retained longer.

No cleanup job is installed or enabled by this release. Any future cleanup must be reviewed, backed up, scoped only to raw product events, and run separately from the web process.

Payment transactions, billing records, subscription history, audit logs, feedback, and operational monitor history are outside the raw product-event retention policy.

## Query performance

The migration adds indexes for:

- `(event_name, created_at)`
- `(user_id, created_at)`
- `created_at`
- `(status, created_at DESC)` on feedback
- `launch_cohort` on users

These indexes cover event funnels, per-user activity, time-window retention, open feedback, and cohort filters without adding an index for every event property. Review production query plans with `EXPLAIN (ANALYZE, BUFFERS)` after enough production data exists. Do not claim production performance from development data.

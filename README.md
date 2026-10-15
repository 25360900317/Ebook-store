# PromptShelf AI — Python eBook Store Starter

A Flask starter for an eBook store with a polished responsive UI, Razorpay Checkout, an order-specific personal UPI QR, admin-reviewed personal-UPI transfers, webhook-based payment confirmation, expiring download links, SMTP email delivery, and an admin dashboard.

## 0. Important status
This is a **starter project**, not a production-audited payment system. It is not connected to a live merchant account. Use Razorpay test mode first. Before accepting real money, add CSRF protection and rate limiting, test webhook retries/failures, configure HTTPS, use persistent private file storage, secure admin access, and review applicable tax/privacy/consumer requirements.

## 1. Run locally
Python 3.10+ recommended.

```bash
python -m venv .venv
# Windows:
.venv\\Scripts\\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env
# macOS/Linux: cp .env.example .env
```

Edit `.env`. Set a long random `FLASK_SECRET_KEY`. Keep `.env` private. Add your PDF into `private_books/` named `sample-ebook.pdf` or upload a PDF via the admin panel after login.

Create an admin password hash:

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('REPLACE_WITH_A_STRONG_PASSWORD'))"
```

Put the printed hash into `ADMIN_PASSWORD_HASH`, and change `ADMIN_USERNAME`. Do not use the sample password in production.

Run:
```bash
flask --app app run --debug
```
Open http://127.0.0.1:5000

## 2. Razorpay test setup
1. Create/verify your Razorpay merchant account and use **test mode** keys.
2. Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env`.
3. The webhook must be reachable from the internet. For local testing, use a secure tunnel such as ngrok; do not expose your local app without protection.
4. In Razorpay Dashboard, add webhook URL `https://YOUR-TEST-URL/webhooks/razorpay` and subscribe to `payment.captured`. Set a webhook secret and put the exact same value in `RAZORPAY_WEBHOOK_SECRET`.
5. Test successful, failed, duplicate and delayed webhooks. The checkout callback does **not** grant access; only a valid `payment.captured` webhook matching the expected amount marks an order paid.
6. Confirm current gateway rules and availability of UPI / QR methods for your merchant account. Never trust a screenshot or browser redirect as proof of payment.

## 3. Email
For Gmail SMTP, enable 2-Step Verification and use a Google App Password if your account supports it. Use that app password in `SMTP_PASSWORD`; never put your normal Gmail password into code. Set:
- `SMTP_HOST=smtp.gmail.com`
- `SMTP_PORT=587`
- `SMTP_USERNAME=youraddress@gmail.com`
- `SMTP_PASSWORD=your-app-password`
- `SMTP_FROM=youraddress@gmail.com`

SMTP may have sending limits. For a business, consider a transactional email provider. If SMTP is not configured, the order can be marked paid but the email will not be sent; the admin can retry after fixing email settings.

## 4. Product files
Keep eBooks in `private_books/`, which is not a public static directory. Admin uploads accept PDF only and are limited by the configured request size. The download URL contains a random token stored only as a hash in the database and expires after `DOWNLOAD_TOKEN_HOURS`.

## 5. Routes
- `/` — storefront
- `/checkout/<product_id>` — create gateway order and open checkout
- `/webhooks/razorpay` — signed webhook receiver
- `/download/<token>` — secure expiring download
- `/admin/login` — admin login
- `/admin` — product/order dashboard

## 6. Before production
- Set `FLASK_DEBUG=0`; use a production WSGI server such as Gunicorn.
- Set a secure `FLASK_SECRET_KEY`, strong admin password hash, HTTPS `SITE_URL`, and private persistent storage.
- Use a managed database and private object storage if deploying on ephemeral free hosting.
- Add CSRF protection, login throttling, security monitoring, backups, privacy policy, terms, refund/contact details and proper error handling.
- Make email delivery retryable via a job queue/outbox; current starter logs SMTP failure and supports manual resend from admin.
- Consider making webhook event processing transactional and reconciling captured payments periodically.
- Validate PDF MIME/content and upload quotas beyond the extension check.
- Do not commit `.env`, keys, customer data, or paid eBooks to a public repository.


## 7. UPI and QR clarification
The checkout includes both: (a) Razorpay Checkout, where supported UPI/QR methods depend on your Razorpay merchant account; and (b) a dynamic order-specific QR generated for the personal UPI ID configured in `.env` (`UPI_ID=saxenajiiiii@axl` by default). The QR includes the order amount and order note. Customers who use the personal UPI QR submit their transaction reference, which changes the order to `manual_pending`; it does not unlock delivery. In Admin → Recent orders, verify the actual credit and amount in your bank/UPI app before clicking **Verify credit & deliver**. That admin action marks the order paid and emails the secure link. Never trust screenshots or a customer claim alone. Never ask for or store a UPI PIN or OTP.

Automatic delivery remains tied to a valid Razorpay `payment.captured` webhook whose order and amount match. A personal UPI QR does not provide an automatic verified webhook on its own. For fully automatic QR delivery on every payment, use a merchant/payment-provider QR product that sends signed server-side payment events and implement its specific webhook contract. Never place gateway secret keys in frontend JavaScript.

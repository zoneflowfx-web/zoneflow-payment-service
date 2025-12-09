import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe
import requests

# -----------------------------
# CONFIG
# -----------------------------

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VIP_GROUP_ID = os.getenv("VIP_GROUP_ID")  # Telegram VIP group/channel id (with -100... etc)

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# Price IDs
PRICE_ID_MONTHLY = os.getenv("PRICE_ID_MONTHLY")
PRICE_ID_QUARTERLY = os.getenv("PRICE_ID_QUARTERLY")
PRICE_ID_YEARLY = os.getenv("PRICE_ID_YEARLY")

# Map our plan keys to Stripe price IDs
PLAN_PRICE_MAP = {
    "monthly": PRICE_ID_MONTHLY,
    "quarterly": PRICE_ID_QUARTERLY,
    "yearly": PRICE_ID_YEARLY,
}

# In-memory subscription store (keyed by telegram_user_id as string)
SUBSCRIPTIONS = {}

# -----------------------------
# FLASK APP
# -----------------------------

app = Flask(__name__)
CORS(app)


@app.route("/", methods=["GET"])
def health():
    return "OK – ZoneFlow payment service", 200


# -----------------------------
# HELPERS
# -----------------------------

def create_single_use_invite_link() -> str | None:
    """
    Asks Telegram to create a one-time invite link for the VIP group.
    member_limit = 1  => only one person can join with this link
    """
    if not TELEGRAM_BOT_TOKEN or not VIP_GROUP_ID:
        print("Telegram token or VIP_GROUP_ID not set.")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/createChatInviteLink"
    payload = {
        "chat_id": VIP_GROUP_ID,
        "member_limit": 1,
        "creates_join_request": False,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print("Failed to create invite link:", data)
            return None
        return data["result"]["invite_link"]
    except Exception as e:
        print("Error calling Telegram API:", e)
        return None


def send_payment_confirmed_message(telegram_user_id: int, plan: str, invite_link: str | None):
    """
    Sends a DM to the user from your bot with the VIP invite link.
    """
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not configured; cannot send message.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    if invite_link:
        text = (
            "🎉 Payment Confirmed!\n\n"
            "Welcome to ZoneFlow FX VIP!\n\n"
            f"Plan: {plan}\n\n"
            "Here is your private VIP group link:\n"
            f"{invite_link}\n\n"
            "If you have any problem joining, reply to this message. 🚀"
        )
    else:
        text = (
            "🎉 Payment Confirmed!\n\n"
            "Welcome to ZoneFlow FX VIP!\n\n"
            "We could not generate an automatic invite link.\n"
            "Please contact support so we can manually add you. 🙏"
        )

    payload = {
        "chat_id": telegram_user_id,
        "text": text,
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            print("Failed to send Telegram message:", r.text)
    except Exception as e:
        print("Error sending Telegram message:", e)


def record_subscription(telegram_user_id: int, plan: str, status: str, current_period_end: int | None):
    """
    Store minimal subscription info in memory for /admin endpoints.
    In production you'd use a database; for this project in-memory is enough.
    """
    SUBSCRIPTIONS[str(telegram_user_id)] = {
        "plan": plan,
        "status": status,
        "current_period_end": current_period_end,
    }


# -----------------------------
# CREATE CHECKOUT SESSION
# -----------------------------

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """
    Body: { "telegram_user_id": 654..., "plan": "monthly|quarterly|yearly" }
    Returns: { "checkout_url": "https://checkout.stripe.com/..." }
    """
    data = request.get_json(silent=True) or {}

    telegram_user_id = data.get("telegram_user_id")
    plan = data.get("plan")

    if not telegram_user_id or not plan:
        return jsonify({"error": "telegram_user_id and plan are required"}), 400

    price_id = PLAN_PRICE_MAP.get(plan)
    if not price_id:
        return jsonify({"error": f"Invalid plan '{plan}'"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url="https://t.me/ZoneFlowFXBot?start=success",
            cancel_url="https://t.me/ZoneFlowFXBot?start=cancel",
            metadata={
                "telegram_user_id": str(telegram_user_id),
                "plan": plan,
            },
            subscription_data={
                "metadata": {
                    "telegram_user_id": str(telegram_user_id),
                    "plan": plan,
                }
            },
        )
    except Exception as e:
        print("Error creating checkout session:", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"checkout_url": session.url}), 200


# -----------------------------
# STRIPE WEBHOOK
# -----------------------------

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("⚠️  Webhook error:", e)
        return "Bad payload", 400

    event_type = event["type"]
    obj = event["data"]["object"]

    # For logging
    print("🔔 Received event:", event_type)

    # We care most about invoice payment success (subscription active)
    if event_type in ("invoice.payment_succeeded", "invoice.payment_paid"):
        try:
            # Get subscription and metadata
            subscription_id = obj.get("subscription")
            if subscription_id:
                subscription = stripe.Subscription.retrieve(subscription_id)
                metadata = subscription.metadata or {}
            else:
                metadata = obj.get("metadata", {})

            telegram_user_id = metadata.get("telegram_user_id")
            plan = metadata.get("plan", "unknown")

            if telegram_user_id:
                telegram_user_id_int = int(telegram_user_id)

                # Current period end (epoch)
                current_period_end = None
                if subscription_id:
                    cpe = getattr(subscription, "current_period_end", None)
                    if cpe:
                        current_period_end = int(cpe)

                # Record in our in-memory dict
                record_subscription(
                    telegram_user_id_int,
                    plan,
                    status="active",
                    current_period_end=current_period_end,
                )

                # Create single-use invite link
                invite_link = create_single_use_invite_link()

                # Send Telegram DM
                send_payment_confirmed_message(
                    telegram_user_id_int,
                    plan,
                    invite_link,
                )

        except Exception as e:
            print("Error handling invoice.payment_succeeded:", e)

    # You can add more handlers for subscription updates or cancellations if needed.

    return "OK", 200


# -----------------------------
# ADMIN ENDPOINTS
# -----------------------------

def _check_admin_auth(req) -> bool:
    header_key = req.headers.get("X-Admin-Key", "")
    return header_key == ADMIN_API_KEY and ADMIN_API_KEY


@app.route("/admin/subscriptions", methods=["GET"])
def admin_subscriptions():
    if not _check_admin_auth(request):
        return jsonify({"error": "unauthorised"}), 401
    return jsonify(SUBSCRIPTIONS), 200


@app.route("/admin/subscription/<telegram_id>", methods=["GET"])
def admin_subscription(telegram_id):
    if not _check_admin_auth(request):
        return jsonify({"error": "unauthorised"}), 401

    info = SUBSCRIPTIONS.get(str(telegram_id))
    if not info:
        return jsonify({"error": "not found"}), 404

    return jsonify(info), 200


# -----------------------------
# ENTRY POINT
# -----------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5005"))
    app.run(host="0.0.0.0", port=port)

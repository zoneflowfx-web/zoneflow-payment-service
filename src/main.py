import os
import json
import stripe
import requests
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# ---------------------------------
# LOAD ENVIRONMENT VARIABLES
# ---------------------------------
load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VIP_CHANNEL_ID_RAW = os.getenv("VIP_CHANNEL_ID")  # string from .env
VIP_STATIC_INVITE_LINK = os.getenv("VIP_STATIC_INVITE_LINK")  # fallback link

TEST_TELEGRAM_USER_ID = os.getenv("TEST_TELEGRAM_USER_ID")  # for stripe trigger tests

# VIP channel ID as int if available
VIP_CHANNEL_ID = int(VIP_CHANNEL_ID_RAW) if VIP_CHANNEL_ID_RAW else None

stripe.api_key = STRIPE_SECRET_KEY

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = Flask(__name__)

# File to persist subscription -> telegram mapping
SUBSCRIPTIONS_FILE = Path(__file__).resolve().parent / "subscriptions.json"


# ---------------------------------
# UTILS: SUBSCRIPTIONS STORAGE
# ---------------------------------
def load_subscriptions() -> dict:
    if not SUBSCRIPTIONS_FILE.exists():
        return {}
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("Error loading subscriptions.json:", e)
        return {}


def save_subscriptions(data: dict) -> None:
    try:
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("Error saving subscriptions.json:", e)


# ---------------------------------
# TELEGRAM HELPERS
# ---------------------------------
def send_telegram_message(chat_id, text: str):
    """
    Sends a message to a Telegram user or chat.
    """
    if not TELEGRAM_BOT_TOKEN:
        print("⚠ TELEGRAM_BOT_TOKEN is not set.")
        return

    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        print("Telegram sendMessage response:", r.text)
    except Exception as e:
        print("Error sending Telegram message:", e)


def create_single_use_invite_link() -> str | None:
    """
    Create a fresh, single-use invite link for the VIP channel.
    Requires the bot to be admin in the VIP group.
    """
    if not VIP_CHANNEL_ID:
        print("⚠ VIP_CHANNEL_ID is not set, cannot create invite link.")
        return VIP_STATIC_INVITE_LINK

    url = f"{TELEGRAM_API_BASE}/createChatInviteLink"
    payload = {
        "chat_id": VIP_CHANNEL_ID,
        "member_limit": 1,  # single-use link
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if not data.get("ok"):
            print("Failed to create invite link:", data)
            # fallback to static link if set
            return VIP_STATIC_INVITE_LINK
        return data["result"]["invite_link"]
    except Exception as e:
        print("Error creating invite link:", e)
        return VIP_STATIC_INVITE_LINK


def get_vip_invite_link() -> str | None:
    """
    For real payments we prefer a fresh single-use link.
    If Telegram API fails, use static fallback.
    For test events we might still end up here.
    """
    link = create_single_use_invite_link()
    if link:
        return link
    if VIP_STATIC_INVITE_LINK:
        return VIP_STATIC_INVITE_LINK
    print("⚠ No VIP invite link available.")
    return None


def remove_user_from_vip(telegram_user_id: str):
    """
    Remove (kick) a user from the VIP channel.
    """
    if not VIP_CHANNEL_ID:
        print("⚠ VIP_CHANNEL_ID not set; cannot remove user.")
        return

    # Newer Telegram API uses banChatMember/unbanChatMember pattern
    ban_url = f"{TELEGRAM_API_BASE}/banChatMember"
    unban_url = f"{TELEGRAM_API_BASE}/unbanChatMember"

    payload = {"chat_id": VIP_CHANNEL_ID, "user_id": int(telegram_user_id)}

    try:
        r = requests.post(ban_url, json=payload, timeout=10)
        print("Telegram banChatMember response:", r.text)
        # Immediately unban so they are just kicked, not permanently banned
        r2 = requests.post(unban_url, json=payload, timeout=10)
        print("Telegram unbanChatMember response:", r2.text)
    except Exception as e:
        print("Error removing user from VIP:", e)


# ---------------------------------
# CHECKOUT SESSION CREATION
# ---------------------------------
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    data = request.json or {}

    telegram_user_id = data.get("telegram_user_id")
    plan = data.get("plan")  # "monthly", "quarterly", "yearly"

    if not telegram_user_id or not plan:
        return jsonify({"error": "Missing required fields"}), 400

    # Stripe Price IDs
    PRICE_IDS = {
        "monthly": "price_1SbrNsLs179MoCfO5dwM0Cmh",
        "quarterly": "price_1SbrOvLs179MoCfOt2we2JcS",
        "yearly": "price_1SbrPiLs179MoCfO8Vp9sXmA",
    }

    if plan not in PRICE_IDS:
        return jsonify({"error": "Invalid plan"}), 400

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[
                {
                    "price": PRICE_IDS[plan],
                    "quantity": 1,
                }
            ],
            success_url="https://t.me/ZoneFlowFXBot?start=success",
            cancel_url="https://t.me/ZoneFlowFXBot?start=cancel",
            metadata={
                "telegram_user_id": str(telegram_user_id),
                "plan": plan,
            },
        )

        return jsonify({"checkout_url": session.url})

    except Exception as e:
        print("Error creating checkout session:", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------
# STRIPE WEBHOOK
# ---------------------------------
@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        print("⚠ Webhook secret missing.")
        return "Webhook secret not set", 400

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        print("❌ Webhook error:", e)
        return "Invalid signature", 400

    event_type = event["type"]
    obj = event["data"]["object"]

    print("✅ Stripe event:", event_type)

    # -------------------------
    # PAYMENT SUCCESS
    # -------------------------
    if event_type == "checkout.session.completed":
        session = obj
        metadata = session.get("metadata", {}) or {}
        telegram_user_id = metadata.get("telegram_user_id")
        plan = metadata.get("plan", "test-plan")
        subscription_id = session.get("subscription")  # may be None in tests

        print("🔥 Payment metadata:", metadata)
        print("Subscription ID:", subscription_id)

        # Fallback for test events
        if not telegram_user_id and TEST_TELEGRAM_USER_ID:
            telegram_user_id = TEST_TELEGRAM_USER_ID
            print("ℹ Using TEST_TELEGRAM_USER_ID:", telegram_user_id)

        # Store subscription -> telegram mapping (for real subscriptions)
        if subscription_id and telegram_user_id:
            subs = load_subscriptions()
            subs[str(subscription_id)] = str(telegram_user_id)
            save_subscriptions(subs)
            print("💾 Stored subscription mapping:", subs)

        if telegram_user_id:
            invite_link = get_vip_invite_link()

            welcome_text = (
                "🎉 Payment Confirmed!\n\n"
                "Welcome to ZoneFlow FX VIP!\n\n"
                f"Plan: {plan}\n\n"
                "Here is your private VIP group link:\n"
                f"{invite_link if invite_link else 'Invite link not configured – please contact support.'}\n\n"
                "If you have any problem joining, reply to this message. 🚀"
            )

            send_telegram_message(chat_id=telegram_user_id, text=welcome_text)
        else:
            print(
                "⚠ No telegram_user_id in metadata and no TEST_TELEGRAM_USER_ID – cannot send VIP access."
            )

    # -------------------------
    # SUBSCRIPTION CANCELED / ENDED
    # -------------------------
    elif event_type == "customer.subscription.deleted":
        subscription = obj
        subscription_id = str(subscription.get("id"))
        print("🧾 Subscription deleted:", subscription_id)

        subs = load_subscriptions()
        telegram_user_id = subs.pop(subscription_id, None)
        save_subscriptions(subs)

        if telegram_user_id:
            print("Removing user from VIP for ended subscription:", telegram_user_id)
            remove_user_from_vip(telegram_user_id)
            send_telegram_message(
                chat_id=telegram_user_id,
                text=(
                    "❌ Your ZoneFlow FX VIP subscription has ended or been cancelled.\n\n"
                    "Access to the VIP group has been removed.\n"
                    "You can rejoin anytime by purchasing a new plan through the bot."
                ),
            )
        else:
            print("No stored Telegram user for subscription:", subscription_id)

    # -------------------------
    # SUBSCRIPTION RENEWAL SUCCESS
    # -------------------------
    elif event_type == "invoice.payment_succeeded":
        invoice = obj
        subscription_id = invoice.get("subscription")
        print("💸 Invoice payment succeeded. Subscription:", subscription_id)

        if subscription_id:
            subs = load_subscriptions()
            telegram_user_id = subs.get(str(subscription_id))
            if telegram_user_id:
                send_telegram_message(
                    chat_id=telegram_user_id,
                    text=(
                        "✅ Your ZoneFlow FX VIP subscription has been renewed successfully.\n\n"
                        "Your VIP access remains active. 🚀"
                    ),
                )
            else:
                print(
                    "No Telegram user stored for subscription on renewal:",
                    subscription_id,
                )

    return "OK", 200


# ---------------------------------
# SIMPLE HEALTH CHECK
# ---------------------------------
@app.route("/", methods=["GET"])
def home():
    return "Payment service active"


if __name__ == "__main__":
    print("Payment service running...")
    port = int(os.getenv("PORT", 5005))
    app.run(host="0.0.0.0", port=port)


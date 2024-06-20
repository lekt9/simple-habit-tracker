from difflib import SequenceMatcher
import json
import os
import logging
import requests
from telegram import Update, Bot
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)
import pytz
from retry import retry
from pymongo import MongoClient
from minio import Minio
from dotenv import load_dotenv
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY")
# WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_URL = "https://n8n.socrathink.com/webhook-test/__EMPTY__/webhook/habit"
# Initialize MongoDB client
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["habit"]
users_collection = db["users"]

# Initialize MinIO client
minio_client = Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=True,
)


# Initialize Telegram bot
bot = Bot(token=TELEGRAM_TOKEN)

# Scheduler for reminders
scheduler = BackgroundScheduler(timezone=pytz.utc)  # or any other timezone you prefer
scheduler.start()


def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Welcome! Tell me your habit and I'll help you stay accountable. "
        "For example, you can say 'I want to eat healthy' or 'I want to lose weight'.\n Use /check_progress to see how you are faring."
    )


def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    text = update.message.text

    # Check if user exists
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        # Create a new user
        users_collection.insert_one(
            {
                "user_id": user_id,
                "habits": [],
                "events": [],
                "journal_entries": [],
                "last_activity": datetime.now(),
            }
        )

    # Fetch all available habits
    all_habits = get_all_habits()
    user_habits = None

    # Fetch the last 20 pieces of evidence
    last_20_evidences = list(
        users_collection.find_one(
            {"user_id": user_id}, {"events": {"$slice": -20}}
        ).get("events", [])
    )

    if update.message.photo:
        photo_file = update.message.photo[-1].get_file()
        photo_path = f"{photo_file.file_id}.jpg"

        # Download photo
        photo_file.download(photo_path)

        # Upload photo to MinIO
        minio_client.fput_object("habit", photo_path, photo_path)

        # Process photo with OpenAI
        response, score = process_with_openai(
            """Check if this photo is valid evidence for the user's habit and provide a score from -10 to 10 if it can support the habit as evidence it is worked on, or disprove that the habit was broken. Go with -10 if it completely goes against the habit. Return a json object in this format:
            {
                "habit": "running every day",
                "information": {
                    "pace": "12min/km",
                    "duration": "60mins",
                    "distance": "5km",
                },
                "message": "Great run! You're on your journey to getting fitter and healthier. Good on you!",
                "points": 6
            }
            You MUST match the habit string's value to be exactly the same as the closest habit available that I provided you.
            when checking evidence, you will also know the last 20 evidences as conext as some evidences will require knowledge of previous habits - ie if we want to track how much food eaten in a day, we need to see the prevoius meals of the day, to warn the user they are reaching the limit.
            You MUST follow the same keys as presented by previous evidences to keep things consistent. You may add new keys only if they are needed.
            Today's date is:
            """
            + datetime.today().strftime("%B %d, %Y"),
            photo_path,
            all_habits,
            last_20_evidences,
        )

        if response["points"] > -10:
            # Check if the habit exists for the user
            user_habits = users_collection.find_one({"user_id": user_id}).get(
                "habits", []
            )
            existing_habit = None
            for habit in user_habits:
                if (
                    similar(habit["name"].lower(), response["habit"].lower()) > 0.8
                ):  # Adjust the similarity threshold as needed
                    existing_habit = habit
                    break

            if existing_habit:
                # Habit exists, update the points and log the event
                users_collection.update_one(
                    {"user_id": user_id, "habits.name": existing_habit["name"]},
                    {
                        "$inc": {"habits.$.points": score},
                        "$set": {"last_activity": datetime.now()},
                        "$push": {
                            "events": {
                                "type": "photo",
                                "timestamp": datetime.now(),
                                "response": response,
                                "score": score,
                                "habit_type": "photo",  # Add habit type here
                            }
                        },
                    },
                    upsert=True,
                )
            else:
                # Habit does not exist, add the habit and log the event
                users_collection.update_one(
                    {"user_id": user_id},
                    {
                        "$addToSet": {
                            "habits": {
                                "name": response["habit"],
                                "points": score,
                                "last_activity": datetime.now(),
                            }
                        },
                        "$push": {
                            "events": {
                                "type": "photo",
                                "timestamp": datetime.now(),
                                "response": response,
                                "score": score,
                                "habit_type": "photo",  # Add habit type here
                            }
                        },
                    },
                    upsert=True,
                )

            # Send webhook with the current object
            current_object = users_collection.find_one({"user_id": user_id})
            send_webhook(current_object)

            update.message.reply_text(
                f"Evidence received and processed. Your points have been updated by {score} points.\n{response['message']}"
            )

            # Update pinned message
            update_pinned_message(user_id, update.message.chat_id)
        else:
            update.message.reply_text(
                f"The photo does not seem to be valid evidence for your habit. {response['message']}"
            )
    else:
        # Use LLM to decide if the text is a journal entry, a new habit, or a regular chat message
        response, is_journal_entry = process_with_openai(
            """Determine if the following text is a journal entry, a new habit, or just a chat message without relation to this habit tracker. Return a json object in this format:
            {
                "type": "journal_entry" | "new_habit" | "chat_message" | "chat_command",
                "message": "This is a journal entry." | "This is a new habit." | "This is a chat message/command."
            }"""
            + text,
            None,
            all_habits,
            last_20_evidences,
        )

        if response["type"] == "journal_entry":
            # Save journal entry to MongoDB
            users_collection.update_one(
                {"user_id": user_id},
                {
                    "$push": {
                        "journal_entries": {"entry": text, "timestamp": datetime.now()}
                    },
                    "$set": {"last_activity": datetime.now()},
                },
                upsert=True,
            )

            update.message.reply_text("Journal entry received. Keep up the good work!")
        elif response["type"] == "new_habit":
            # Check if a similar habit exists
            similar_habit = None
            if user_habits:
                for habit in user_habits:
                    if (
                        similar(habit["name"].lower(), text.lower()) > 0.8
                    ):  # Adjust the similarity threshold as needed
                        similar_habit = habit
                        break

            if similar_habit:
                # Update the existing habit
                users_collection.update_one(
                    {"user_id": user_id, "habits.name": similar_habit["name"]},
                    {
                        "$set": {
                            "habits.$.name": text,
                            "habits.$.last_activity": datetime.now(),
                        },
                        "$setOnInsert": {"habits.$.points": 0},
                    },
                    upsert=True,
                )
                update.message.reply_text(
                    f"Updated your habit to: {text}. Please send evidence of your progress or journal your activities."
                )
            else:
                # Save or update user habit to MongoDB
                habit_type = (
                    "general"  # Default habit type, you can modify this as needed
                )
                users_collection.update_one(
                    {"user_id": user_id},
                    {
                        "$addToSet": {
                            "habits": {
                                "name": text,
                                "type": habit_type,
                                "points": 0,
                                "last_activity": datetime.now(),
                            }
                        },
                        "$set": {"last_activity": datetime.now()},
                    },
                    upsert=True,
                )
                update.message.reply_text(
                    f"Got it! I'll help you stay accountable for: {text}. Please send evidence of your progress or journal your activities."
                )
        else:
            pass

        # Update pinned message
        update_pinned_message(user_id, update.message.chat_id)


def extract_score_from_response(response):

    if "points" in response:
        return response["points"]
    return 0  # Default score if not found


def update_pinned_message(user_id, chat_id):
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        logger.warning(f"User {user_id} not found. Cannot update pinned message.")
        return

    habits = user.get("habits", [])
    message_text = "Your current points:\n"
    total_points = 0
    for habit in habits:
        points = habit.get("points", 0)
        message_text += f"{habit['name']}: {points} points\n"
        total_points += points

    message_text += f"\nTotal points: {total_points} points"

    # Send a new message with the points
    message = bot.send_message(chat_id=chat_id, text=message_text)

    # Pin the message
    bot.pin_chat_message(chat_id=chat_id, message_id=message.message_id)


def get_all_habits():
    habits = users_collection.distinct("habits.name")
    return habits


@retry(tries=3, delay=1)
def process_with_openai(prompt, image_path=None, habits=None, evidences=None):
    messages = [
        {
            "role": "system",
            "content": "You are an AI assistant that helps users stay accountable for their habits by analyzing evidence photos and journal entries. You will reason with the teachings of the book: 'Atomic Habits'",
        },
        {"role": "user", "content": prompt},
    ]

    if habits:
        habits_list = ", ".join(habits)
        messages.append(
            {"role": "user", "content": f"Here are the available habits: {habits_list}"}
        )

    if evidences:
        evidences_list = json.dumps(evidences, default=str)
        messages.append(
            {
                "role": "user",
                "content": f"Here are the last 20 pieces of evidence: {evidences_list}",
            }
        )

    if image_path:
        # Generate the public URL for the image stored in MinIO
        image_url = minio_client.presigned_get_object("habit", image_path)
        print("image_url", image_url)
        messages.append(
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": image_url}}],
            }
        )

    data = {
        "model": "openai/gpt-4o",
        "messages": messages,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data
    )
    print(response.json())
    response.raise_for_status()

    response_content = response.json()["choices"][0]["message"]["content"].replace(
        "```json", "```"
    )
    if "```" in response_content:
        response_content = response_content.split("```")[1]

    response_content = json.loads(response_content)
    # Extract score from response
    score = extract_score_from_response(response_content)

    return response_content, score


def send_reminder():
    users = users_collection.find()
    for user in users:
        user_id = user["user_id"]
        habits = user.get("habits", [])  # Assuming each user can have multiple habits
        events = user.get("events", [])

        habits_not_on_track = []

        for habit in habits:
            habit_name = habit.get("name")
            habit_type = habit.get("type", "general")  # Default to "general" if not set

            # Fetch the last 30 pieces of evidence for this habit
            last_30_evidences = [e for e in events if e["habit_name"] == habit_name][
                :20
            ]
            print("30", last_30_evidences)

            # Use OpenAI to decide if the habit is on track and explain why
            prompt = f'Here are the last 20 pieces of evidence for the habit \'{habit_name}\': {json.dumps(last_30_evidences, default=str)}. Determine if the habit is on track and explain why. Return a json object in this format:\n{{\n"is_on_track": true,\n"message": "The habit is on track.",\n"reason": "Explanation of why the habit is or is not on track."\n}}'
            response, _ = process_with_openai(prompt)

            print("response", response)

            if not response.get("is_on_track", False):
                habits_not_on_track.append(
                    {
                        "habit_name": habit_name,
                        "reason": response.get("reason", "No reason provided"),
                    }
                )
            print("not", habits_not_on_track)

        if habits_not_on_track:
            # Generate a personalized reminder message using OpenAI
            habits_list = ", ".join(
                [
                    f"{habit['habit_name']} (Reason: {habit['reason']})"
                    for habit in habits_not_on_track
                ]
            )
            prompt = f"The user has the following habits that are not on track: {habits_list}. Generate a personalized reminder message to encourage them to stay accountable."
            reminder_message, _ = process_with_openai(prompt)
            bot.send_message(user_id, reminder_message)

            # Update pinned message
            update_pinned_message(
                user_id, user_id
            )  # Assuming user_id is the chat_id for reminders


def check_progress(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id

    # Fetch the user's habits
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        # Create a new user
        users_collection.insert_one(
            {
                "user_id": user_id,
                "habits": [],
                "events": [],
                "journal_entries": [],
                "last_activity": datetime.now(),
            }
        )
        update.message.reply_text(
            "You don't have any habits yet. Start by adding a new habit!"
        )
        return

    habits = user["habits"]
    events = user.get("events", [])

    # Use OpenAI to generate a progress report
    prompt = f'The user has the following habits: {json.dumps(habits, default=str)}. Here are the last 20 pieces of evidence: {json.dumps(events[-20:], default=str)}. Generate a progress report for each habit and provide suggestions for improvement. Return a json object in this format:\n{{\n"progress_report": [\n{{\n"habit": "habit_name",\n"progress": "progress_description",\n"suggestions": "suggestions_for_improvement"\n}}\n]\n}}'
    response, _ = process_with_openai(prompt)

    progress_report = response.get("progress_report", [])
    if not progress_report:
        update.message.reply_text("No progress report available at the moment.")
        return

    # Format the progress report
    report_text = "Here is your progress report:\n"
    for report in progress_report:
        report_text += f"\nHabit: {report['habit']}\nProgress: {report['progress']}\nSuggestions: {report['suggestions']}\n"

    update.message.reply_text(report_text)


def send_webhook(data):
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        logger.error("WEBHOOK_URL is not set in the environment variables.")
        return

    try:
        response = requests.post(webhook_url, json=data)
        response.raise_for_status()
        logger.info("Webhook sent successfully.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send webhook: {e}")


def main():
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("check_progress", check_progress))
    dispatcher.add_handler(MessageHandler(Filters.text | Filters.photo, handle_message))

    # Schedule reminders
    scheduler.add_job(
        send_reminder, "interval", hours=3, timezone=pytz.utc  # Run every 3 hours
    )

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()

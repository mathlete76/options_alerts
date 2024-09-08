import asyncio
import nest_asyncio  # <-- Import nest_asyncio to handle nested event loops
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import pymongo
import ccxt
import constants

# Apply the nest_asyncio patch to allow nested event loops
nest_asyncio.apply()

# Initialize your bot token
TOKEN = constants.BOT_TOKEN


client = pymongo.MongoClient(constants.MONGO_URL,
                            username=constants.MONGO_USER,
                            password=constants.MONGO_PW,
                            authMechanism=constants.MONGO_AUTH)

db = client['trading_bot']
alerts_collection = db['alerts']

# Initialize CCXT for Deribit
exchange = ccxt.deribit({
    'enableRateLimit': True,
})

# Fetch and filter market symbols from Deribit using fetch_markets()
def get_available_symbols():
    try:
        markets = exchange.fetch_markets()  # Fetch detailed market info
        # Filter markets of type 'swap' and with 'BTC' or 'ETH' in their id
        filtered_symbols = [
            market['symbol'] for market in markets 
            if market['type'] == 'swap' and ('BTC' in market['id'] or 'ETH' in market['id'])
        ]
        return filtered_symbols
    except Exception as e:
        print(f"Error loading symbols: {e}")
        return []

# Command to set price alert with inline buttons for filtered BTC/ETH swap symbols
async def set_alert(update: Update, context):
    # Get available symbols from Deribit
    symbols = get_available_symbols()

    # Create inline keyboard buttons with the available symbols
    keyboard = []
    for symbol in symbols:
        keyboard.append([InlineKeyboardButton(symbol, callback_data=symbol)])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text('Please select a symbol:', reply_markup=reply_markup)

# Callback query handler for symbol selection
async def button_click(update: Update, context):
    query = update.callback_query
    symbol = query.data  # The symbol selected by the user

    # Acknowledge the button press
    await query.answer()

    # Ask the user for the target price now
    await query.message.reply_text(f'You selected {symbol}. Please enter the target price:')
    
    # Store the selected symbol in context.user_data for later use
    context.user_data['selected_symbol'] = symbol

# Handle text input for price after symbol selection
async def handle_price_input(update: Update, context):
    if 'selected_symbol' in context.user_data:
        symbol = context.user_data['selected_symbol']
        try:
            price = float(update.message.text)
            chat_id = update.effective_chat.id

            # Get the current price to initialize last_price
            current_price = get_price(symbol)

            # Save the alert in MongoDB with last_price
            alert = {
                'chat_id': chat_id,
                'symbol': symbol,
                'price': price,
                'alerted': False,
                'last_price': current_price  # Store the current price as last_price
            }
            alerts_collection.insert_one(alert)

            await update.message.reply_text(f'Alert set for {symbol} at {price}')
            context.user_data.clear()

        except ValueError:
            await update.message.reply_text('Please enter a valid price.')
    else:
        await update.message.reply_text('Please select a symbol first using /setalert.')

# Command to list all alerts
async def list_alerts(update: Update, context):
    chat_id = update.effective_chat.id
    alerts = list(alerts_collection.find({'chat_id': chat_id, 'alerted': False}))  # Convert cursor to list

    if len(alerts) > 0:  # Check if there are any alerts
        message = "Your alerts:\n"
        for idx, alert in enumerate(alerts):
            message += f"{idx+1}. {alert['symbol']} at {alert['price']}\n"
        await update.message.reply_text(message)
    else:
        await update.message.reply_text("No alerts set.")

# Command to delete an alert
async def delete_alert(update: Update, context):
    try:
        chat_id = update.effective_chat.id
        idx = int(context.args[0]) - 1
        alerts = list(alerts_collection.find({'chat_id': chat_id, 'alerted': False}))

        if 0 <= idx < len(alerts):
            alert_id = alerts[idx]['_id']
            alerts_collection.delete_one({'_id': alert_id})
            await update.message.reply_text("Alert deleted.")
        else:
            await update.message.reply_text("Invalid alert number.")
    
    except (IndexError, ValueError):
        await update.message.reply_text('Usage: /deletealert <alert_number>')

# Fetch price from Deribit using CCXT
def get_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker['last']
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None

# Periodically check prices (using CCXT) and trigger alerts on price crossing
async def check_prices(context):
    alerts = alerts_collection.find({'alerted': False})  # Get all active alerts

    for alert in alerts:
        symbol = alert['symbol']
        target_price = alert['price']
        last_price = alert['last_price']
        current_price = get_price(symbol)

        if current_price is None:
            continue  # Skip if unable to fetch the price

        # Check if the price crossed the target price
        if (last_price > target_price and current_price <= target_price) or (last_price < target_price and current_price >= target_price):
            # Price crossed the target, send alert
            await context.bot.send_message(
                chat_id=alert['chat_id'],
                text=f"Price alert for {symbol}: The price crossed {target_price}. Current price: {current_price}"
            )

            # Mark the alert as triggered
            alerts_collection.update_one({'_id': alert['_id']}, {'$set': {'alerted': True}})
        
        # Update last_price in the database
        alerts_collection.update_one({'_id': alert['_id']}, {'$set': {'last_price': current_price}})

# Main function to run the bot
async def run_bot():
    # Initialize the application with JobQueue enabled
    application = Application.builder().token(TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("setalert", set_alert))
    application.add_handler(CallbackQueryHandler(button_click))  # Handles button clicks
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_price_input))  # Handles price input
    application.add_handler(CommandHandler("listalerts", list_alerts))
    application.add_handler(CommandHandler("deletealert", delete_alert))

    # Start the price check job every 30 seconds
    job_queue = application.job_queue
    job_queue.run_repeating(check_prices, interval=30)

    # Start polling and keep the bot running
    await application.run_polling()

# Main entry point
if __name__ == '__main__':
    asyncio.run(run_bot())  # Simply use asyncio.run to handle everything

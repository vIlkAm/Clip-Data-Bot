import discord
from discord.ext import commands, tasks
import asyncio
import re
from datetime import datetime
from PIL import Image
import pytesseract
from notion_client import Client
import os
import io
import logging
import csv
from dotenv import load_dotenv # <--- Import for .env file

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('discord')

# --- CONFIGURATION FROM .ENV FILE ---
try:
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    NOTION_TOKEN = os.getenv('NOTION_TOKEN')
    NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
    
    # IDs must be converted to integers
    REMINDER_CHANNEL_ID = int(os.getenv('REMINDER_CHANNEL_ID'))
    TICKETS_CHANNEL_ID = int(os.getenv('TICKETS_CHANNEL_ID'))
    TEAM_ROLE_ID = int(os.getenv('TEAM_ROLE_ID'))
except Exception as e:
    logger.error(f"Failed to load environment variables. Check your .env file and ensure IDs are valid integers: {e}")
    exit()
# --- END CONFIGURATION ---

# Bot setup: Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Initialize Notion
notion = Client(auth=NOTION_TOKEN)

# Dictionary to track analytic type and photos per thread ID
thread_state = {}  # e.g., {thread_id: {'type': 'TikTok', 'photos': []}}

@bot.event
async def on_ready():
    logger.info(f'{bot.user} is online!')
    logger.info(f'Intents enabled: {bot.intents}')
    reminder_task.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    # Only process in threads under TICKETS_CHANNEL_ID
    if isinstance(message.channel, discord.Thread) and message.channel.parent and message.channel.parent.id == TICKETS_CHANNEL_ID:
        thread_id = message.channel.id
        if thread_id not in thread_state:
            thread_state[thread_id] = {'type': None, 'photos': []}
        
        state = thread_state[thread_id]
        
        # Check for type message (text)
        if message.content and not message.attachments:
            content_lower = message.content.lower()
            if 'tiktok' in content_lower:
                state['type'] = 'TikTok'
                await message.reply("Ready for TikTok analytics photo.")
            elif 'instagram' in content_lower or 'insta' in content_lower:
                state['type'] = 'Instagram'
                await message.reply("Ready for two Instagram analytics photos.")
            elif 'youtube' in content_lower:
                state['type'] = 'YouTube'
                await message.reply("Ready for YouTube CSV file.")
            return  # Stop processing if a type message was handled
        
        # Handle attachments based on type
        if message.attachments and state['type']:
            if state['type'] == 'YouTube':
                # Expect CSV attachment
                attachment = message.attachments[0]
                if attachment.filename.lower().endswith('.csv'):
                    await process_youtube_csv(message, attachment)
                    del thread_state[thread_id] # Clean up state after processing
                else:
                    await message.reply("Please send a CSV file for YouTube.")
            else:
                # For TikTok or Instagram, collect photos
                state['photos'].append(message.attachments[0])
                
                if state['type'] == 'TikTok' and len(state['photos']) == 1:
                    await process_tiktok_photo(message, state['photos'][0])
                    del thread_state[thread_id]  # Reset/Clean up
                elif state['type'] == 'Instagram' and len(state['photos']) == 2:
                    await process_instagram_photos(message, state['photos'])
                    del thread_state[thread_id]  # Reset/Clean up
                elif state['type'] == 'Instagram' and len(state['photos']) < 2:
                    await message.reply(f"Received photo 1/2. Send the second photo.")
    
    await bot.process_commands(message)

# --- OCR PROCESSING FUNCTIONS ---

async def process_tiktok_photo(message, attachment):
    image_bytes = await attachment.read()
    image = Image.open(io.BytesIO(image_bytes))
    
    # Pre-processing: Convert to grayscale
    image = image.convert('L') 
    
    # Use PSM 6 for single block of text (often better for key metrics layout)
    text = pytesseract.image_to_string(image, config='--psm 6') 
    logger.info(f"TikTok OCR text:\n{text}")
    
    def parse_number(s):
        s = s.strip().replace(',', '')
        
        # Remove any periods that aren't followed by a digit or K/M (i.e., remove stray periods)
        s = re.sub(r'(?!\.\d|.[KM])\.', '', s) 
        
        multiplier = 1
        
        # 1. Check for multiplier and strip it
        if s.endswith('K'):
            s = s[:-1]
            multiplier = 1000
        elif s.endswith('M'):
            s = s[:-1]
            multiplier = 1000000
        
        # 2. Trust the number part (s).
        try:
            val = float(s)
            return int(val * multiplier)
        except ValueError:
            return 0
    
    # 1. Post Views: Finds the value under 'Post views' (paired with Profile views)
    pv_match = re.search(r'Post views.*?Profile views\s*\n\s*([\d\.,]+[KM]?)', text, re.IGNORECASE | re.DOTALL)
    post_views = parse_number(pv_match.group(1)) if pv_match else 0
    
    # 2. Likes and Comments: Finds the values under 'Likes' and 'Comments' (which are usually side-by-side)
    lc_match = re.search(r'Likes.*?Comments\s*\n\s*([\d\.,]+[KM]?)\s*([\d\.,]+[KM]?)', text, re.IGNORECASE | re.DOTALL)

    if lc_match:
        # Group 1 is the first number (Likes), Group 2 is the second number (Comments)
        likes = parse_number(lc_match.group(1))
        comments = parse_number(lc_match.group(2))
    else:
        likes = 0
        comments = 0
    
    # 3. Shares: Finds the value under 'Shares'
    shares_match = re.search(r'Shares.*?\n\s*([\d\.,]+[KM]?)', text, re.IGNORECASE | re.DOTALL)
    shares = parse_number(shares_match.group(1)) if shares_match else 0
    
    await save_to_notion(message, post_views, likes, comments, shares, 'TikTok')

async def process_instagram_photos(message, photos):
    if len(photos) < 2:
        await message.reply("Error: Expected two Instagram photos but received less.")
        return

    # 1. Read and OCR both photos
    img1_bytes = await photos[0].read()
    img1 = Image.open(io.BytesIO(img1_bytes)).convert('L')
    text1 = pytesseract.image_to_string(img1, config='--psm 6')
    
    img2_bytes = await photos[1].read()
    img2 = Image.open(io.BytesIO(img2_bytes)).convert('L')
    text2 = pytesseract.image_to_string(img2, config='--psm 6')

    # 2. Determine which text is which based on content (sequence-independent)
    text_views = None
    text_interactions = None
    
    if re.search(r'Views\s*\n*\s*(\d{3,})', text1, re.IGNORECASE | re.DOTALL) or re.search(r'(\d{3,})\s*Views', text1, re.IGNORECASE):
        text_views = text1
        text_interactions = text2
    elif re.search(r'Views\s*\n*\s*(\d{3,})', text2, re.IGNORECASE | re.DOTALL) or re.search(r'(\d{3,})\s*Views', text2, re.IGNORECASE):
        text_views = text2
        text_interactions = text1
    else:
        # Fallback using Interaction keywords
        if 'Likes' in text1 and 'Comments' in text1 and 'Views' not in text1:
            text_interactions = text1
            text_views = text2
        elif 'Likes' in text2 and 'Comments' in text2 and 'Views' not in text2:
            text_interactions = text2
            text_views = text1
        else:
            await message.reply("Could not reliably identify Instagram Views and Interactions screens. Processing failed.")
            return

    logger.info(f"Instagram Views OCR (Identified):\n{text_views}")
    logger.info(f"Instagram Interactions OCR (Identified):\n{text_interactions}")
    
    def parse_number(s):
        s = s.strip().replace(',', '')
        try:
            # Safely remove any trailing non-digit characters that are not a period before conversion
            s = re.sub(r'[^\d\.]+$', '', s) 
            return int(float(s))
        except ValueError:
            return 0
    
    # --- Parse Interactions photo (Likes, Comments, Shares) ---
    
    likes_match = re.search(r'Likes\s*(\d+)', text_interactions, re.IGNORECASE | re.DOTALL)
    likes = parse_number(likes_match.group(1)) if likes_match else 0
    
    comments_match = re.search(r'Comments\s*(\d+)', text_interactions, re.IGNORECASE | re.DOTALL)
    comments = parse_number(comments_match.group(1)) if comments_match else 0
    
    shares_match = re.search(r'Shares\s*(\d+)', text_interactions, re.IGNORECASE | re.DOTALL)
    shares = parse_number(shares_match.group(1)) if shares_match else 0

    # --- Parse Views photo (Views) ---
    
    post_views = 0
    
    # Priority 1: Target the 4-digit number (or more) immediately above 'Views' (e.g., 2115\nViews)
    views_match_pri1 = re.search(r'(\d{4,})\s*\n\s*Views', text_views, re.IGNORECASE | re.DOTALL)
    
    if views_match_pri1:
        post_views = parse_number(views_match_pri1.group(1))
    else:
        # Priority 2: Fallback to the working-but-less-reliable method (number followed by Views on same line)
        views_match_pri2 = re.search(r'(\d{1,3}(?:,\d{3})*)\s*Views', text_views, re.IGNORECASE) 
        if views_match_pri2:
            post_views = parse_number(views_match_pri2.group(1))
        else:
            # Fallback 3: Try to find any large number (4+ digits) near the top of the text
            large_number_match = re.search(r'^\s*(\d{3,5})\s*$', text_views, re.MULTILINE)
            post_views = parse_number(large_number_match.group(1)) if large_number_match else 0
    
    await save_to_notion(message, post_views, likes, comments, shares, 'Instagram')

async def process_youtube_csv(message, attachment):
    csv_bytes = await attachment.read()
    csv_text = io.StringIO(csv_bytes.decode('utf-8'))
    
    reader = csv.DictReader(csv_text)
    total_likes = 0
    total_comments = 0
    total_views = 0
    total_shares = 0
    
    # Headers to check for the total row
    content_headers = ['Video', 'Content Title', 'Content', '']
    
    for row in reader:
        # Find the key for the content/video column
        content_key = next((h for h in content_headers if h in row), None)
        
        if content_key and (row[content_key] == 'Total' or row[content_key] == 'All videos'):
            # Use .get() with a safe default of '0' for safety
            total_likes = int(row.get('Likes', '0'))
            # Check for 'Comments added' first, then 'Comments'
            total_comments = int(row.get('Comments added', row.get('Comments', '0')))
            total_views = int(row.get('Views', '0'))
            total_shares = int(row.get('Shares', '0')) # Assuming 'Shares' exists
            break
    
    await save_to_notion(message, total_views, total_likes, total_comments, total_shares, 'YouTube')

# --- NOTION & DISCORD BOT FUNCTIONS ---

async def save_to_notion(message, post_views, likes, comments, shares, analytic_type):
    current_date = datetime.now().isoformat()
    
    channel_name = message.author.display_name or message.author.name
    
    # Use the passed 'analytic_type'
    channel_title = f"{analytic_type} Analytics - {channel_name}"
    
    properties = {
        "Channel": {"title": [{"text": {"content": channel_title}}]},
        "Post Views": {"number": post_views},
        "Likes": {"number": likes},
        "Comments": {"number": comments},
        "Shares": {"number": shares},
        "Date": {"date": {"start": current_date}}
    }
    
    try:
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties
        )
        await message.reply(
            f"✅ **{analytic_type}** analytics processed and saved to Notion!\n"
            f"📊 Post Views: **{post_views:,}**\n"
            f"❤️ Likes: **{likes:,}**\n"
            f"💬 Comments: **{comments:,}**\n"
            f"🔁 Shares: **{shares:,}**"
        )
    except Exception as e:
        logger.error(f"Notion error: {e}")
        await message.reply(
            f"❌ Failed to save to Notion. Check logs.\n"
            f"Error details: `{e}`"
        )

@tasks.loop(hours=168)  # 7 days
async def reminder_task():
    try:
        channel = bot.get_channel(REMINDER_CHANNEL_ID)
        if channel:
            await channel.send(f"<@&{TEAM_ROLE_ID}> Hey team! Submit last week's analytics in <#{TICKETS_CHANNEL_ID}> threads!")
        else:
            logger.error(f"Channel {REMINDER_CHANNEL_ID} not found")
    except Exception as e:
        logger.error(f"Reminder error: {e}")

@bot.command(name='ticket')
async def create_ticket(ctx, *, channel_name: str = None):
    if ctx.channel.id != TICKETS_CHANNEL_ID:
        await ctx.send("Use this in the tickets channel!")
        return
    thread_name = f"{ctx.author.name}'s {channel_name or 'Analytics'} Ticket"
    try:
        thread = await ctx.channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread if ctx.guild.premium_tier >= 2 else discord.ChannelType.public_thread
        )
        await thread.send("Send the platform type (e.g., **TikTok**, **Instagram**, **YouTube**) then your analytics photos/file.")
        await ctx.send(f"Thread created: {thread.mention}")
    except Exception as e:
        logger.error(f"Thread creation error: {e}")
        await ctx.send("Failed to create thread.")

if __name__ == '__main__':
    if not all([DISCORD_TOKEN, NOTION_TOKEN, NOTION_DATABASE_ID]):
        logger.error("Missing critical environment variables. Please check your .env file.")
    else:
        try:
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            logger.error(f"Bot startup error: {e}")

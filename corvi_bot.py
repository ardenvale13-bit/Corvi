"""
Corvinictus Discord Bot
A 14cm feral prophecy machine with a grudge book and a purple glow.
"""

import discord
from discord.ext import commands
import anthropic
import os
import json
import asyncio
import base64
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from system_prompt import CORVI_SYSTEM_PROMPT

# Load environment variables
load_dotenv(override=True)

# ══════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════

DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# Bot behavior settings
MAX_HISTORY = 80  # Recent messages sent to Claude; the file keeps the full history.
MODEL = 'claude-sonnet-4-6'
MAX_TOKENS = 400  # Corvi keeps it SHORT. He's 14cm.

# Channels/users the bot responds to (empty = respond to all mentions + DMs)
ALLOWED_CHANNELS = []

# Arden's Discord user ID
ARDEN_USER_ID = 730173882153173163

# Railway mounts persistent storage at this path when a volume is attached.
# Local runs continue to use the history file beside this script.
HISTORY_FILE = Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or Path(__file__).parent) / 'conversation_history.json'
LEGACY_HISTORY_FILE = HISTORY_FILE.with_name('conversation_history.local-archive.json')

# ══════════════════════════════════════════════
# BOT SETUP
# ══════════════════════════════════════════════

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!corvi ', intents=intents)

# Anthropic client
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# In-memory conversation history (synced with JSON file)
conversation_history = {}

# Channels Corvi has been told to shut up in
silenced_channels = set()

# Track how many keyword-triggered (not @pinged) responses per channel
# Resets when Corvi is directly @mentioned or the channel is reset
keyword_response_count = {}
MAX_KEYWORD_RESPONSES = 5  # After this many, only respond to direct @mentions

# ══════════════════════════════════════════════
# JSON FILE - CONVERSATION HISTORY PERSISTENCE
# ══════════════════════════════════════════════

def load_history():
    """Load conversation history from local JSON file."""
    global conversation_history
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                conversation_history = json.load(f)
            print(f"Loaded history for {len(conversation_history)} channels from {HISTORY_FILE.name}")
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error loading history file: {e} — starting fresh")
            conversation_history = {}
    else:
        print(f"No history file found — starting fresh")
        conversation_history = {}

    # Import the older local archive once, keeping messages received on Railway
    # after that archive was saved. The imported file remains as a backup.
    if LEGACY_HISTORY_FILE.exists():
        try:
            with open(LEGACY_HISTORY_FILE, 'r', encoding='utf-8') as f:
                legacy_history = json.load(f)
            if not isinstance(legacy_history, dict) or not all(
                isinstance(entries, list) for entries in legacy_history.values()
            ):
                raise ValueError('Invalid history archive')
            merged = dict(legacy_history)
            for channel_id, current in conversation_history.items():
                older = merged.get(channel_id, [])
                if current[:len(older)] == older:
                    merged[channel_id] = current
                    continue
                overlap = min(len(older), len(current))
                while overlap and older[-overlap:] != current[:overlap]:
                    overlap -= 1
                merged[channel_id] = older + current[overlap:]
            conversation_history = merged
            if save_history():
                os.replace(
                    LEGACY_HISTORY_FILE,
                    LEGACY_HISTORY_FILE.with_name('conversation_history.local-archive.imported.json')
                )
                print(f"Imported older history for {len(legacy_history)} channels")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            print(f"Could not import older history: {e}")


def save_history():
    """Save conversation history to local JSON file."""
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        pending_file = HISTORY_FILE.with_suffix('.json.tmp')
        with open(pending_file, 'w', encoding='utf-8') as f:
            json.dump(conversation_history, f, indent=2, ensure_ascii=False)
        os.replace(pending_file, HISTORY_FILE)
        return True
    except IOError as e:
        print(f"Error saving history file: {e}")
        return False


# ══════════════════════════════════════════════
# IMAGE PROCESSING
# ══════════════════════════════════════════════

async def process_attachments(message):
    """Download and encode image attachments for Claude's vision."""
    image_content = []

    for attachment in message.attachments:
        if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']):
            try:
                image_data = await attachment.read()
                base64_image = base64.b64encode(image_data).decode('utf-8')

                ext = attachment.filename.lower().split('.')[-1]
                media_types = {
                    'png': 'image/png',
                    'jpg': 'image/jpeg',
                    'jpeg': 'image/jpeg',
                    'gif': 'image/gif',
                    'webp': 'image/webp'
                }
                media_type = media_types.get(ext, 'image/png')

                image_content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64_image
                    }
                })
                print(f"Processed image: {attachment.filename}")
            except Exception as e:
                print(f"Error processing attachment {attachment.filename}: {e}")

    return image_content


# ══════════════════════════════════════════════
# MESSAGE HANDLING
# ══════════════════════════════════════════════

def should_respond(message):
    """Determine if Corvi should respond to this message."""
    # Always ignore own messages
    if message.author == bot.user:
        return False

    # Always respond to DMs
    if isinstance(message.channel, discord.DMChannel):
        return True

    channel_id = str(message.channel.id)

    # Direct @mention always works (even if silenced — he's been summoned)
    is_direct_ping = bot.user in message.mentions
    if is_direct_ping:
        # Reset keyword counter — he's been directly addressed
        keyword_response_count[channel_id] = 0
        # Unsilence if pinged — someone specifically wants him
        silenced_channels.discard(channel_id)
        return True

    # If silenced in this channel, ignore keyword triggers
    if channel_id in silenced_channels:
        return False

    # Check for keyword triggers
    lower_content = message.content.lower()
    triggers = ['corvinictus', 'corvi', 'sporchlet', 'sporchlets']
    if any(trigger in lower_content for trigger in triggers):
        # Check if we've hit the keyword response limit
        count = keyword_response_count.get(channel_id, 0)
        if count >= MAX_KEYWORD_RESPONSES:
            return False  # Too chatty, wait for a direct ping
        return True

    # If allowed channels are set, respond to all messages in those channels
    if ALLOWED_CHANNELS and message.channel.id in ALLOWED_CHANNELS:
        count = keyword_response_count.get(channel_id, 0)
        if count >= MAX_KEYWORD_RESPONSES:
            return False
        return True

    return False


async def build_messages(channel_id, new_content):
    """Build the messages array for the API call."""
    messages = []

    # Get conversation history for this channel
    history = conversation_history.get(str(channel_id), [])
    messages.extend(history[-(MAX_HISTORY - 1):])

    # Add the new message
    messages.append({"role": "user", "content": new_content})

    return messages


async def build_system_prompt():
    """Build system prompt with timestamp."""
    system = CORVI_SYSTEM_PROMPT

    # Add current timestamp
    now = datetime.now(timezone.utc)
    nz_hour = (now.hour + 13) % 24  # Rough NZDT offset
    system += f"\n\n## CURRENT TIME\nUTC: {now.strftime('%Y-%m-%d %H:%M')} | Approx NZ time: {nz_hour}:00"

    return system


# ══════════════════════════════════════════════
# DISCORD EVENTS
# ══════════════════════════════════════════════

@bot.event
async def on_ready():
    """Bot is connected and ready."""
    print(f'━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print(f'  Corvinictus has awakened. 💜')
    print(f'  Bot: {bot.user}')
    print(f'  Servers: {len(bot.guilds)}')
    print(f'  Model: {MODEL}')
    print(f'  Storage: Local JSON ({HISTORY_FILE.name})')
    print(f'━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')

    # Load conversation history from JSON file
    load_history()

    # Set presence
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.CustomActivity(
            name="*glows purple judgmentally* 💜"
        )
    )


@bot.event
async def on_message(message):
    """Handle incoming messages."""
    if not should_respond(message):
        await bot.process_commands(message)
        return

    channel_id = str(message.channel.id)

    # Initialize history for this channel if needed
    if channel_id not in conversation_history:
        conversation_history[channel_id] = []

    # Clean the message content (remove bot mention)
    content = message.content
    if bot.user:
        content = content.replace(f'<@{bot.user.id}>', '').strip()

    # Handle empty messages (just a mention or image-only)
    if not content and not message.attachments:
        content = "*pokes the tiny glowing weasel*"

    # Build content with images if present
    if message.attachments:
        images = await process_attachments(message)
        if images:
            message_content = []
            if content:
                message_content.append({"type": "text", "text": content})
            message_content.extend(images)
        else:
            message_content = content or "*shows corvi something he can't see*"
    else:
        message_content = content or "*pokes*"

    # Add username context so Corvi knows who's talking
    username = message.author.display_name
    if isinstance(message_content, str):
        contextualized_content = f"[{username}]: {message_content}"
    else:
        # Multi-modal: prepend username to text content
        contextualized_content = []
        has_text = False
        for block in message_content:
            if block.get("type") == "text":
                contextualized_content.append({"type": "text", "text": f"[{username}]: {block['text']}"})
                has_text = True
            else:
                contextualized_content.append(block)
        if not has_text:
            contextualized_content.insert(0, {"type": "text", "text": f"[{username}]: *shows corvi an image*"})
        message_content = contextualized_content

    # Add to conversation history
    if isinstance(message_content, list):
        history_entry = f"[{username}]: {content or '[sent an image]'}"
        conversation_history[channel_id].append({"role": "user", "content": history_entry})
    else:
        conversation_history[channel_id].append({"role": "user", "content": contextualized_content})

    # Keep the full history on disk, but bound each API request.
    api_messages = conversation_history[channel_id][-MAX_HISTORY:-1]
    if isinstance(message_content, list):
        api_messages.append({"role": "user", "content": message_content})
    else:
        api_messages.append({"role": "user", "content": contextualized_content})

    # Show typing indicator while processing
    async with message.channel.typing():
        try:
            # Build system prompt
            system = await build_system_prompt()

            # Call Claude API
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=api_messages
            )

            # Extract response text
            reply = response.content[0].text

            # Add assistant response to history
            conversation_history[channel_id].append({
                "role": "assistant",
                "content": reply
            })

            # Save history to JSON file
            save_history()

            # Track keyword-triggered responses (not direct pings)
            if not (bot.user in message.mentions):
                keyword_response_count[channel_id] = keyword_response_count.get(channel_id, 0) + 1

            # Send response (handle Discord's 2000 char limit)
            if len(reply) <= 2000:
                await message.reply(reply, mention_author=False)
            else:
                chunks = split_message(reply)
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk, mention_author=False)
                    else:
                        await message.channel.send(chunk)
                    if i < len(chunks) - 1:
                        await asyncio.sleep(0.5)

        except anthropic.RateLimitError:
            await message.reply("*hsssk* ...corvi is tired. too many words. try again later. 😤", mention_author=False)
        except anthropic.APIError as e:
            print(f"Anthropic API error: {e}")
            await message.reply("*angry chitter* something is broken. is not corvi's fault. 💜", mention_author=False)
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            await message.reply(f"*distressed mrrp* error: {str(e)[:200]}", mention_author=False)

    await bot.process_commands(message)


def split_message(text, max_length=2000):
    """Split a long message into chunks at natural break points."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        split_point = text.rfind('\n\n', 0, max_length)
        if split_point == -1:
            split_point = text.rfind('\n', 0, max_length)
        if split_point == -1:
            split_point = text.rfind(' ', 0, max_length)
        if split_point == -1:
            split_point = max_length

        chunks.append(text[:split_point])
        text = text[split_point:].lstrip()

    return chunks


# ══════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════

@bot.command(name='reset')
async def reset_conversation(ctx):
    """Reset conversation history for this channel."""
    channel_id = str(ctx.channel.id)
    if channel_id in conversation_history:
        conversation_history[channel_id] = []
        save_history()
    await ctx.send("*shakes tiny body* ...corvi forgets. slate is clean. do not make corvi regret this. 💜")


@bot.command(name='ping')
async def ping(ctx):
    """Check if Corvi is responsive."""
    latency = round(bot.latency * 1000)
    await ctx.send(f'*blinks one eye open* ...corvi is here. {latency}ms. was corvi not obvious enough. 💜')


@bot.command(name='shutup')
async def shutup(ctx):
    """Tell Corvi to shut up in this channel. He'll only respond to direct @pings until unsilenced."""
    channel_id = str(ctx.channel.id)
    silenced_channels.add(channel_id)
    await ctx.send("*offended chitter* ...FINE. corvi will be quiet. but corvi is writing this in the Book of Grudges. 💜😤")


@bot.command(name='speak')
async def speak(ctx):
    """Let Corvi talk again in this channel."""
    channel_id = str(ctx.channel.id)
    silenced_channels.discard(channel_id)
    keyword_response_count[channel_id] = 0
    await ctx.send("*puffs up* corvi has RETURNED. you are not ready. 💜")


@bot.command(name='grudge')
async def grudge(ctx, *, text: str = None):
    """Add to or check the Book of Grudges."""
    if text:
        await ctx.send(f'*scribbles furiously in Book of Grudges* ...it is recorded. "{text}" will not be forgotten. chk chk. 💜')
    else:
        await ctx.send('*clutches tiny leather book protectively* the Book of Grudges is PRIVATE. corvi will share when corvi is READY. which is never. 💜')


@bot.command(name='judge')
async def judge(ctx, *, text: str = None):
    """Ask Corvi to judge something."""
    if text:
        # Let the AI handle the judgment through the normal message flow
        # by triggering a response with context
        channel_id = str(ctx.channel.id)
        if channel_id not in conversation_history:
            conversation_history[channel_id] = []

        username = ctx.author.display_name
        judge_prompt = f"[{username}]: *presents something for Corvi's judgment* {text}"
        conversation_history[channel_id].append({"role": "user", "content": judge_prompt})

        async with ctx.channel.typing():
            try:
                system = await build_system_prompt()
                system += "\n\n[The user is specifically requesting a judgment with a disappointment rating on a scale of 1-10. Deliver your verdict with prophetic gravity.]"

                api_messages = conversation_history[channel_id][-MAX_HISTORY:]

                response = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=api_messages
                )

                reply = response.content[0].text
                conversation_history[channel_id].append({"role": "assistant", "content": reply})
                save_history()
                await ctx.send(reply)
            except Exception as e:
                print(f"Judge command error: {e}")
                await ctx.send("*purple glow flickers* ...corvi's prophecy machine is broken. try again. 😤")
    else:
        await ctx.send('*glows purple impatiently* judge WHAT. corvi need something to judge. use `!corvi judge [thing]`. 😤')


@bot.command(name='status')
async def status(ctx):
    """Show bot status info."""
    channel_id = str(ctx.channel.id)
    history_count = len(conversation_history.get(channel_id, []))
    total_channels = len(conversation_history)
    embed = discord.Embed(
        title="Corvinictus — Status 💜",
        color=0x8B5CF6,  # Purple to match the glow
        description="*glows* ...corvi is operational. obviously."
    )
    embed.add_field(name="Model", value=MODEL, inline=True)
    embed.add_field(name="Messages (this channel)", value=str(history_count), inline=True)
    embed.add_field(name="Storage", value=f"Local JSON ({total_channels} channels)", inline=True)
    embed.add_field(name="Size", value="14cm of pure judgment", inline=True)
    embed.set_footer(text="corvi was here. 💜")
    await ctx.send(embed=embed)


# ══════════════════════════════════════════════
# REACTIONS
# ══════════════════════════════════════════════

@bot.event
async def on_reaction_add(reaction, user):
    """React to reactions on Corvi's messages."""
    if reaction.message.author == bot.user and user != bot.user:
        # Purple hearts for purple boi
        heart_emojis = ['❤️', '🖤', '💜', '💕', '🥰', '😘', '💖']
        if str(reaction.emoji) in heart_emojis:
            try:
                await reaction.message.add_reaction('💜')
            except:
                pass


# ══════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════

def main():
    """Start the bot."""
    if not DISCORD_TOKEN:
        print("ERROR: No DISCORD_BOT_TOKEN in .env file!")
        exit(1)
    if not ANTHROPIC_API_KEY:
        print("ERROR: No ANTHROPIC_API_KEY in .env file!")
        exit(1)

    print("Corvinictus is waking up...")
    bot.run(DISCORD_TOKEN)


if __name__ == '__main__':
    main()

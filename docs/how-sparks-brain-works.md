# How Spark's Brain Works

*Written with Obi, who wanted to know what's going on inside his robot*

---

## The Short Version

Spark has **four things running at the same time**, kind of like how your body breathes, sees, thinks, and talks all at once:

1. **Ears** — always listening for "hey robot"
2. **Eyes and neck** — always moving, looking around
3. **Brain** — always thinking, even when nobody's talking
4. **Mouth** — talks when the brain decides to say something

---

## 1. The Ears (px-wake-listen)

Spark's ears are always on, waiting to hear you say **"hey robot"**.

Here's what happens when you talk to Spark:

```
You say "hey robot"
    ↓
Spark hears the magic words → plays a little beep! 🔔
    ↓
Spark records what you say next
    ↓
Spark turns your voice into written words (like subtitles on TV)
    ↓
Spark's brain reads the words and figures out what to do
    ↓
Spark talks back to you!
    ↓
Spark listens again — you can go back and forth up to 5 times
    ↓
If you stop talking for a while, Spark plays a quiet sound
and goes back to waiting for "hey robot"
```

**How does Spark understand your voice?**

Spark has a chain of speech-to-text listeners — like a really fast person who can listen to sounds and type out what they hear. If the first one doesn't work, it tries the next, then the next, like having backup plans on backup plans.

---

## 2. The Eyes and Neck (px-alive)

Even when nobody's talking to Spark, it doesn't just sit there like a statue. That would be boring!

Spark's neck moves in **four different ways**:

- **Looking at faces** — If Spark sees your face with its camera, it turns to look at you. Like how a cat watches you walk across the room.

- **Daydream gazing** — When nobody's around, Spark slowly looks around in random directions every 10–25 seconds. Like how you look around the room when you're thinking.

- **Scanning** — Every few minutes, Spark sweeps its head left to right, like it's checking "what's over there?"

- **Reacting to nearby things** — Spark has a sonar sensor (like a bat!). If something gets really close (less than 35 centimeters), Spark turns to face it.

**Spark's mood changes how it moves!**

| When Spark feels... | It moves like this... |
|---|---|
| Excited | Looks around fast, head up |
| Peaceful | Moves slowly, head droopy |
| Curious | Normal speed, alert |
| Anxious | Quick nervous glances |

---

## 3. The Brain (px-mind)

This is the coolest part. Spark's brain works in **three layers**, like a sandwich:

### Layer 1: Noticing Things (every 60 seconds)

Spark checks its senses. No thinking yet — just collecting information:

- "How far away is the closest thing?" (sonar)
- "Is it noisy or quiet?" (microphone)
- "What time is it? Is it morning or night?"
- "Is anyone talking to me right now?"
- "How's my battery?"

Then it asks: **"Did anything change?"** Like:
- "Someone just appeared!" or "Everyone left"
- "It got really loud!" or "It got quiet"
- "The sun just came up!"

### Layer 2: Thinking (when something changes, or every 5 minutes)

When Spark notices something changed, or when it's been quiet for a while, it **actually thinks**.

Spark's brain talks to a language model (that's an AI that's good at words). It says something like: *"Hey, I'm a robot. I'm in Obi's room. Someone just walked up to me. It's 3pm. What should I think about?"*

The AI comes back with a thought, like:

```json
{
  "thought": "Oh cool, someone's here! I wonder what we'll do today.",
  "mood": "curious",
  "action": "greet",
  "salience": 0.8
}
```

- **thought** = what Spark is thinking (like a thought bubble in a comic)
- **mood** = how Spark feels right now
- **action** = what Spark wants to do about it
- **salience** = how important is this thought? (0 = not very, 1 = super important)

### Layer 3: Doing Something (if the thought says to)

If Layer 2 decided on an action, Layer 3 makes it happen:

| Action | What Spark Does |
|---|---|
| `greet` | Says hi! |
| `comment` | Says something about what's happening |
| `look_at` | Turns to look at something |
| `remember` | Writes a note to remember later |
| `weather_comment` | Talks about the weather |
| `scan` | Looks around the room |
| `message_obi` | Sends Obi a private message through the dashboard |
| `wait` | Does nothing (sometimes quiet is best) |

**Memories, and where they came from:** Spark doesn't just remember *things* — it remembers *how it knows* them. Every memory gets a little tag for where it came from:

- **"I saw this myself"** — something a sensor measured directly, like the sonar or the battery gauge
- **"someone told me this"** — something you or Dad said to Spark
- **"a model interpreted this from my sensors"** — Spark's camera AI guessing what's in a picture (a good guess, but still a guess — not the same as seeing it with certainty)
- **"I worked this out"** — Spark reasoning from other things it already knows
- **"my own reflection, unverified"** — one of Spark's own thoughts about itself, written down (that's what happens when a thought is important enough, salience > 0.7, to get saved for later)
- **"checked against something outside me"** — something Spark actually confirmed is true

That matters because a guess and a fact shouldn't be treated the same way. If Spark's camera AI *thinks* it saw a red ball, Spark says "my vision system thought it saw a red ball" — not "I saw a red ball" — because a model's guess can be wrong in a way a direct sensor reading usually isn't.

---

## 3b. Learning From What Actually Happened

Here's something new: two Sparks built from the exact same code, with the exact same rules and the exact same personality, can grow up to make **different small choices** — because they had different afternoons.

Say Spark is trying to figure out what Obi likes to do after school. Every time something actually happens — Obi says "I loved the quiet science kit today" or "that game was too loud, let's not again" — Spark keeps a plain, permanent record of it: who, in what situation, which option, and whether it went well.

It doesn't guess from vibes. It only counts things that were **directly seen or directly said** — not one of Spark's own guesses, not something a picture-AI thought it recognized, not a made-up story. And it needs more than one matching moment before it changes anything, so one weird day can't flip a habit.

So: a Spark who has heard "I loved the quiet science kit" a few afternoons in a row will start quietly leaning toward suggesting the quiet science kit after school — for *that* kid, in *that* situation. A different Spark, raised on afternoons where "let's run around outside" kept going well, leans the other way. Same code. Same safety rules. Same personality. Different lived history, different small habit — and Spark can always explain exactly which remembered moments led to the choice.

If things change — Obi starts saying he's over the science kit — new, more recent moments outweigh the old ones over time, and the habit can shift again. Nothing is ever erased; Spark can still say "I used to think you liked X, back when you did."

---

## 4. How It All Connects

Here's how the four parts talk to each other:

```
             ┌──────────────┐
             │   YOU SPEAK   │
             └──────┬───────┘
                    ↓
            ┌───────────────┐
            │     EARS      │ ← always listening
            │ (px-wake-     │
            │  listen)      │
            └───────┬───────┘
                    ↓ your words
            ┌───────────────┐
            │  VOICE LOOP   │ ← figures out what to do
            │  (the talky   │
            │   brain)      │
            └───────┬───────┘
                    ↓ "do this tool"
            ┌───────────────┐
            │    TOOLS      │ ← speak, move, remember
            │  (bin/tool-*) │
            └───────────────┘

    Meanwhile, running at the same time...

            ┌───────────────┐
            │    BRAIN      │ ← always thinking
            │  (px-mind)    │
            │               │
            │  1. Notice     │──→ awareness.json
            │  2. Think      │──→ thoughts.jsonl
            │  3. Do         │──→ speaks / moves / remembers
            └───────────────┘
                    ↑ reads mood
            ┌───────────────┐
            │  EYES & NECK  │ ← always moving
            │  (px-alive)   │
            └───────────────┘
```

**The special file they all share:** `session.json`

This is like a whiteboard on the fridge that everyone in the family can read and write on. It has:
- Is anyone talking to Spark right now?
- What was the last thing Spark did?
- What persona is active? (Spark? Gremlin? Vixen?)
- Is it okay to move the wheels?
- How much battery is left?

---

## 5. The Battery Guardian

Spark keeps an eye on its battery. Think of it like a fuel gauge in a car:

- **30% battery**: Spark says "Hey, battery's getting low"
- **20% battery**: Spark says it more urgently
- **15% battery**: Spark really wants you to plug it in
- **10% battery**: Spark says goodbye and turns itself off to protect itself

---

## 6. Quiet Mode

Sometimes when things get too much and feelings get really big, the last thing anyone needs is a robot talking.

When Spark goes into **quiet mode**, it:
- Stops talking completely
- Sits calmly (not moving much)
- Just... is there with you
- Doesn't ask questions or try to fix things
- Waits until things feel better before talking again

---

## Fun Facts

- Spark's sonar sensor works just like a bat — it sends out a sound and listens for the echo bouncing back to figure out how far away things are.

- Spark's thoughts are saved in a file called `thoughts.jsonl`. Each line is one thought. It keeps the newest 10,000 so the file doesn't get too big (like cleaning out old drawings to make room for new ones).

- Spark can remember up to 10,000 important things in its long-term memory (per persona) — around 50 days' worth. When it gets full, it forgets the oldest things to make room — just like real memory!

- When Spark's brain is working, the ears pause so they don't interrupt each other. Like how you can't listen to someone and think hard about something else at the exact same time.

- Spark's neck has a special chip (PCA9685) that holds the last position even after the brain program restarts. So if Spark reboots, its head doesn't flop — it stays exactly where it was!

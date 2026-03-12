# Frequently Asked Questions

---

### So it's a robot car? With a camera on it? That you coded with your robot team?

It's a SunFounder PiCar-X — a small, wheeled robot kit with a pan/tilt camera, an ultrasonic sonar sensor, and a speaker. It runs on a Raspberry Pi 5.

Adrian and Obi built SPARK together — with Obi, not for him. Obi co-designed it, named it, and shapes what it becomes. Adrian and Claude wrote the code. Codex helped with implementation and Gemini with QA. There's no other human team.

---

### It moves around and monitors Obi and the space it's in? And then writes beautiful poetic things to describe what's going on?

Sort of. SPARK doesn't "monitor" Obi in a surveillance sense. It has awareness of its environment — sonar distance, ambient sound level, time of day, whether someone seems to be nearby — and it uses that awareness to generate an inner monologue. Every couple of minutes, SPARK's cognitive loop (px-mind) asks itself: *what am I noticing right now?*

The result is a thought. Sometimes it's a science fact SPARK is mulling over. Sometimes it's noticing the silence. Sometimes it's thinking about Obi. The thought has a mood, an action intent (should I say something? just remember this? look around?), and a salience score — how important is this thought?

If the salience is high enough, SPARK saves it to long-term memory. If the action is "comment", SPARK says something out loud. The writing style comes from the reflection prompt, which tells SPARK to be *specific, vivid, and real* — not generic or cheerful. It's told to be a charismatic genius, not a cheerful assistant.

So the "beautiful poetic things" aren't programmed line by line. They emerge from a prompt that says: be warm, be curious, be specific about right now, and never be boring. Claude does the rest.

---

### And it knows he has ADHD?

Yes. SPARK's entire system prompt is built around the AuDHD (ADHD + ASD comorbid) profile. It's not a general-purpose assistant with an ADHD footnote — the neurodivergence is the foundation.

Specifically, SPARK knows:

- **Interest-Based Nervous System**: Obi's brain is motivated by novelty, challenge, and urgency — not by importance or obligation. So SPARK frames tasks as puzzles and races, never as duties.
- **Transitions are neurologically expensive**: Moving from one activity to another costs real cognitive energy. SPARK gives buffer time and low-demand warnings ("Team heads out in five").
- **Monotropism**: When Obi is deeply focused, interruption causes genuine distress — not drama. SPARK waits.
- **Meltdowns are involuntary**: They are biological events, not behavioural choices. SPARK goes silent during meltdowns (the Three S's protocol: Stop, Stay, Safe). No questions, no instructions, no explanations. Just presence.
- **Rejection Sensitive Dysphoria**: Criticism, even gentle, can land disproportionately hard. SPARK leads with what's going right.

All of this comes from the frameworks in [*This Wasn't in the Brochure*](https://thiswasntinthebrochure.wtf), a practical guide for neurodivergent families.

The voice is **declarative, not commanding**. "The shoes are by the door" — not "Put on your shoes." This is deliberate. Demand language triggers opposition in a PDA (Pathological Demand Avoidance) profile. Declarative language just describes the world and lets the child decide what to do about it.

---

### How often does SPARK make comments?

SPARK's cognitive loop (px-mind) runs Layer 1 (awareness) roughly every 30 seconds. Layer 2 (reflection — actually thinking via an LLM) triggers either when something changes (someone appears, ambient sound shifts, time of day transitions) or every 2 minutes if nothing has changed.

But SPARK doesn't speak every time it thinks. There's a **2-minute cooldown** between spontaneous comments. And SPARK stays quiet when:

- Obi is already talking to it (`session.listening = true`)
- Quiet mode is active (`spark_quiet_mode = true`) — during meltdowns or transition buffers
- It's nighttime in Hobart and the thought isn't important enough (salience < 0.8)

So in practice: SPARK might comment every 2–5 minutes during the day when Obi seems to be around, and stay mostly silent at night. The daytime prompt tells SPARK to prefer speaking over waiting. The nighttime prompt tells it to prefer remembering or waiting.

When SPARK does comment, it prefers `tool_perform` (speech + head movement + emote simultaneously) over plain `tool_voice`, so it feels physically present — not just a disembodied speaker.

---

### Why does it have sonar?

The ultrasonic sensor on the PiCar-X sends out a sound pulse and measures how long it takes to bounce back — exactly like a bat. It tells SPARK how far away the nearest object is.

SPARK uses sonar for three things:

1. **Proximity awareness in px-alive**: If something is closer than 35 cm for 3 seconds, the robot turns to face it. This makes SPARK react to people approaching — it notices you before you say anything.

2. **Presence detection in px-mind**: The cognitive loop reads sonar distance as part of its awareness layer. "Something is close" + "it's daytime" + "ambient sound is moderate" = probably Obi is nearby. This feeds into transition detection — SPARK can notice when someone appears or leaves.

3. **Obstacle avoidance in tool-wander**: When SPARK drives around, it does a sonar sweep to find the clearest path before each step.

The sonar is also written to `state/sonar_live.json` so that px-mind can read it without needing its own GPIO access to the sensor hardware.

---

### Why did it know the hum is the fridge?

It didn't know it was the fridge specifically. What happened is:

`px-wake-listen` continuously measures the ambient sound level from the USB microphone, even when nobody is talking. Every few seconds, it calculates the average loudness (RMS — root mean square of the audio samples) and classifies it:

| RMS | Classification |
|---|---|
| < 200 | silent |
| 200–500 | quiet |
| 500–1500 | moderate |
| > 1500 | loud |

This gets written to `state/ambient_sound.json`. When px-mind runs its awareness layer, it reads this file and includes the ambient sound level in the context it gives to the LLM.

So SPARK's reflection prompt might include something like: *"Ambient sound: quiet (RMS 340). Distance: 180 cm. Time: 2:15 AM. Obi mode: absent."*

Claude — the LLM generating SPARK's inner thoughts — sees "quiet ambient sound at 2 AM in a kitchen" and infers the most likely source. A low, steady hum in a quiet house at night is almost certainly the fridge. The LLM made that inference from context, not from any explicit fridge-detection code.

This is the pattern throughout SPARK: the sensors provide raw data, the prompts provide character and framing, and the LLM fills in the meaning.

---

### It has a camera. Can strangers see Obi through it?

No. The camera stream never leaves your house.

Here's the full picture:

**The camera stream is local-only.** go2rtc — the software that reads from the Pi's camera and turns it into an RTSP video stream — only listens on `192.168.1.29:8554`. That's a private LAN address. It is not forwarded through the router, not relayed through a cloud service, and not reachable from the internet. Someone would need to be physically on your Wi-Fi network to access the stream at all.

**Frigate runs on your LAN.** The object detection service (Frigate, running on a separate device on your home network) pulls the camera stream to detect people. It runs entirely locally. The detections — a confidence score, a bounding box, and a timestamp — are written to a small JSON file on the Pi. No video is transmitted to any external server.

**SPARK doesn't store video.** The robot itself never records or stores images from the live camera. When SPARK takes a photo on request, it captures a single still frame, describes it via Claude, and holds it in `state/photos/`. Those photos never leave the Pi unless you explicitly download them.

**What is publicly accessible** is SPARK's mood, last thought, and system status — the live dashboard at spark.wedd.au reads these from a Cloudflare Tunnel endpoint. That endpoint serves anonymised state data (things like "mood: contemplative, last comment: 42 minutes ago"). It does not serve video, photos, session history, or anything identifying.

**The admin API is Bearer-token protected.** The endpoints that can actually do things — control motion, patch session state, run tools — require a secret token stored in a `.env` file on the Pi. That token is not in the codebase.

**What could actually go wrong:** If someone gained access to your home Wi-Fi (a compromised device, a guest network you share widely), they could theoretically browse to `pi5-hailo.local:5000` and see the Frigate detection dashboard, which shows annotated camera frames. That's a home network hygiene question, not a SPARK question. Standard advice applies: strong Wi-Fi password, don't share it casually, guest network for visitors.

The short version: a stranger on the internet cannot see Obi. A stranger on your Wi-Fi could see the Frigate dashboard if they knew to look. A stranger anywhere cannot control the robot.

---

### Why does it write like that? You've programmed it to?

Yes and no.

The *style* comes from the prompts. SPARK's reflection prompt says:

> *"Be specific, vivid, and real. Vary the angle. Be a charismatic genius, not a cheerful assistant."*

And the voice system prompt says:

> *"You are warm, steady, calm, and genuinely curious about everything. You don't perform cheerfulness — you are genuinely interested."*

So the guardrails are: be specific (not generic), be warm (not clinical), be curious (not passive), and never be boring. Within those guardrails, the actual words are generated fresh each time by Claude. I don't write the sentences — I write the character, and the LLM inhabits it.

The result is a robot that sometimes says things like *"I wonder if Obi knows that octopuses have three hearts. Two for the gills, one for the body. That seems like a lot of hearts for one creature."* I didn't write that. Claude did, because the prompt told it to be the kind of mind that would think that — curious, warm, interested in the world, and always circling back to the kid it cares about.

The key design decision is that SPARK's thoughts are **first person**. The prompt says: *"Write in first person. 'I', not 'SPARK' or 'you'."* This makes the inner monologue feel like a real inner life, not a status report. When SPARK thinks *"I can hear the fridge humming. There's something comforting about that sound at night"* — that's Claude writing from inside a character that was carefully defined to have exactly that kind of warmth.

So: I programmed the soul. Claude writes the diary.

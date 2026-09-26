# FastAutomate v2 app: simple UI, zero-tap connect, bubble, pause

*2026-09-26. Approved in chat ("build it an keep it simple").*

## What changes

1. **Stamped download.** `/app/FastAutomate-v2.apk?t=<invite>` serves the APK with the invite token
   stored as an extra ID-value pair (id `0x46414155`) in the APK Signing Block, the Walle method: the
   v2 signature stays valid because the signing block is not covered by it. On first launch the app
   reads its own APK (`applicationInfo.sourceDir`); if a token is there and the phone has none yet,
   it connects to the built-in dashboard address. The token is single-use on the server as today.
   The invite page's Download button uses the stamped link. The Connect link keeps working.
2. **New launcher screen `HomeActivity`**, three tabs:
   - **Home**: a setup card (only while something is missing: Accessibility, Notifications,
     battery, dashboard) where each row opens the exact Android page and ticks itself when granted;
     on sideloaded Android 13+ the Accessibility row explains "⋮ → Allow restricted settings". Then a
     "What should I do?" box + Run, and a live card for the current run (step, Pause/Resume, Stop).
   - **Tasks**: this phone's runs (both origins); tap opens the existing task details screen.
   - **Settings**: status, "Show bubble" switch, **Advanced** = the old `MainActivity`, unchanged.
3. **Bubble (Messenger chat head).** Drawn by the accessibility service (`TYPE_ACCESSIBILITY_OVERLAY`,
   no extra permission), moved by springs (androidx dynamicanimation, Rebound-style): pops in, flings
   to the nearest edge, is pulled onto the bottom ✕ to hide (Settings brings it back). No shadow, no
   dot, hidden on the lock screen. Tap = the chat (`FaChat`, also an overlay window, not an activity:
   the app underneath stays, the status bar stays) grows out of the bubble, 80% of the screen tall, a
   soft dim behind it; tap the bubble again, outside, or Back = it folds back in. A centered heads row:
   the bubble, one head per chat (tap = switch, drag down onto the ✕ = delete, "+" = new chat), always
   scrolled to the latest message. "+" opens Messenger's chat list (New chat, every chat with its last
   message); the row shows the 4 most recent. With "Display over other apps" (a setup step) the chat is an
   application overlay, under Android's gesture bar and back arrow; without it, an accessibility overlay
   that leaves the bottom gesture area free. Back hides the keyboard first; drafts are kept per chat;
   Home or switching apps closes the chat. The app has a Chats tab (opens a chat in the bubble), and every
   chat line reaches the dashboard (`agent/chat`, acked like run reports) and shows in the phone viewer. While a task runs the bubble is red with a stop sign (tap = stop) and
   a small grey pause/play button hangs under it; they step aside only when the agent taps or swipes on
   them (`OverlayShy`), and the executor is told to ignore the bubble in screenshots.
4. **Talking layer (`ChatBrain`).** You chat with the assistant (the planner model, this phone's key);
   each chat keeps its history (`ChatStore`, up to 5 chats), task results included. When something
   should happen on the phone it calls `run_task` with one self-contained instruction, the chat folds
   away and the agent (planner, then executor) runs it on the app underneath; the result lands in the chat.
5. **Pause.** `agent/pause` / `agent/resume {uuid}` from the dashboard, the same from the bubble and
   Home. The loop pauses between steps; paused time does not count toward the time limit. The phone
   reports `paused` / `resumed` events. The dashboard stores the run as `paused` (still active),
   holds the silence watchdog while paused, restarts it on resume, and shows Pause/Resume buttons.

## Not included

Play Store publishing (install referrer comes with it). v1 untouched.

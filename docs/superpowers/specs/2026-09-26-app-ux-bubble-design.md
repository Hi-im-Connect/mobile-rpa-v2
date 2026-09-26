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
3. **Bubble.** Drawn by the accessibility service (`TYPE_ACCESSIBILITY_OVERLAY`, no extra
   permission). Draggable, snaps to the edge, drag onto the bottom ✕ hides it (Settings brings it
   back). Idle tap opens a small dialog activity (text + Run); the dialog closes before the run
   starts so the task works on the app the user was in. While running it shows a progress ring;
   tap shows the step with Pause/Resume and Stop. It is hidden during the agent's screenshots and
   gestures so the agent never sees or taps it.
4. **Pause.** `agent/pause` / `agent/resume {uuid}` from the dashboard, the same from the bubble and
   Home. The loop pauses between steps; paused time does not count toward the time limit. The phone
   reports `paused` / `resumed` events. The dashboard stores the run as `paused` (still active),
   holds the silence watchdog while paused, restarts it on resume, and shows Pause/Resume buttons.

## Not included

Play Store publishing (install referrer comes with it). v1 untouched.

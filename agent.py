name: Daily Hotel & Market Intelligence Agent

on:
  # Started at 9:15 AM CT each weekday by cron-job.org calling the GitHub API
  # (GitHub's own schedule trigger is delayed or dropped too often).
  workflow_dispatch:
    inputs:
      dry_run:
        description: 'Build the email but do not send it (uploads a preview)'
        type: boolean
        default: false

jobs:
  run-daily-digest:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Run only the cron that matches Central time today
        id: gate
        run: |
          TZNAME=$(TZ=America/Chicago date +%Z)
          SCHED="${{ github.event.schedule }}"
          RUN=true
          if [ "$SCHED" = "15 14 * * 1-5" ] && [ "$TZNAME" != "CDT" ]; then RUN=false; fi
          if [ "$SCHED" = "15 15 * * 1-5" ] && [ "$TZNAME" != "CST" ]; then RUN=false; fi
          echo "Central zone: $TZNAME, schedule: '$SCHED', run: $RUN"
          echo "run=$RUN" >> "$GITHUB_OUTPUT"

      - name: Check out repo code
        if: steps.gate.outputs.run == 'true'
        uses: actions/checkout@v4

      - name: Set up Python
        if: steps.gate.outputs.run == 'true'
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Dependencies
        if: steps.gate.outputs.run == 'true'
        run: |
          python -m pip install --upgrade pip
          pip install yfinance feedparser requests google-genai

      - name: Run Market Agent
        if: steps.gate.outputs.run == 'true'
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          SENDER_EMAIL: ${{ secrets.SENDER_EMAIL }}
          RECEIVER_EMAIL: ${{ secrets.RECEIVER_EMAIL }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          DRY_RUN: ${{ inputs.dry_run && '1' || '' }}
        run: python agent.py

      - name: Upload preview (dry run only)
        if: steps.gate.outputs.run == 'true' && inputs.dry_run
        uses: actions/upload-artifact@v4
        with:
          name: brief-preview
          path: brief_preview.html

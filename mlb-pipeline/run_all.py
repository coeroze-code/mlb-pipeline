"""
Runs the full pipeline end to end: batters, then pitchers, then combine.

Used by:
  - .github/workflows/daily_run.yml (scheduled run)
  - streamlit_app.py's "Run Pipeline Now" button (manual run)

Both call run_all() so there's exactly one place the actual sequence lives.
"""

import combine_results
import mlb_pipeline
import probable_pitchers


def run_all():
    print("=== Batters ===")
    try:
        mlb_pipeline.main()
    except Exception as e:
        print(f"  Batters step failed, continuing anyway: {e}")

    print("\n=== Pitchers ===")
    try:
        probable_pitchers.main()
    except Exception as e:
        print(f"  Pitchers step failed, continuing anyway: {e}")

    print("\n=== Combine ===")
    combine_results.main()


if __name__ == "__main__":
    run_all()

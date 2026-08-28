import os
import json
import logging
import anthropic
from pathlib import Path

log = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = Path("prompts/system_prompt.txt").read_text()
MODEL         = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS    = int(os.getenv("ANTHROPIC_MAX_TOKENS", 4096))

def call_claude(user_prompt: str, retries: int = 3) -> dict:
    """
    Send prompt to Claude API and return parsed JSON response.
    Retries up to 3 times on failure with exponential backoff.
    """
    import time

    for attempt in range(1, retries + 1):
        try:
            log.info(f"Claude API call — attempt {attempt}/{retries}")

            message = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_prompt}
                ]
            )

            raw_text = message.content[0].text
            log.info(f"Claude responded — tokens used: "
                     f"in={message.usage.input_tokens} "
                     f"out={message.usage.output_tokens}")

            # Parse JSON (strip any accidental markdown fences)
            clean = raw_text.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            parsed = json.loads(clean.strip())
            if "customer_commentary" not in parsed:
                parsed["customer_commentary"] = get_fallback_response()[
                    "customer_commentary"
                ]
            log.info("Claude response parsed successfully")
            return parsed

        except json.JSONDecodeError as e:
            log.error(f"JSON parse failed on attempt {attempt}: {e}")
            log.debug(f"Raw response: {raw_text[:500]}")
            if attempt == retries:
                return get_fallback_response()

        except anthropic.RateLimitError:
            wait = 60 * attempt
            log.warning(f"Rate limit hit — waiting {wait}s")
            time.sleep(wait)

        except anthropic.APIError as e:
            log.error(f"Anthropic API error: {e}")
            if attempt == retries:
                return get_fallback_response()
            time.sleep(10 * attempt)

    return get_fallback_response()


def get_fallback_response() -> dict:
    """Returns a minimal safe response when Claude API fails."""
    log.error("All Claude API attempts failed — using fallback response")
    return {
        "executive_summary": ["Claude API unavailable — report generated with raw data only"],
        "critical_alerts": [],
        "kpi_commentary": "AI commentary unavailable this week.",
        "production_commentary": "AI commentary unavailable this week.",
        "customer_commentary": "AI commentary unavailable this week.",
        "vendor_commentary": "AI commentary unavailable this week.",
        "market_commentary": "AI commentary unavailable this week.",
        "recommendations": [],
        "outlook": "AI outlook unavailable this week."
    }


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate API cost in USD — update prices from Anthropic pricing page."""
    input_cost  = (input_tokens  / 1_000_000) * 3.00   # $3/M input tokens
    output_cost = (output_tokens / 1_000_000) * 15.00  # $15/M output tokens
    return round(input_cost + output_cost, 4)

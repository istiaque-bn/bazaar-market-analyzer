"""Phase 9: request correlation IDs for structured logging."""
from __future__ import annotations

import uuid
import re

from config.logging_utils import request_id_var

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Adopts an inbound X-Request-ID (so a request that already has one
    from an upstream proxy/load balancer keeps it end to end) or mints a
    short one, stores it in a ContextVar for the duration of the request
    so every log line emitted while handling it can be tagged, and
    echoes it back on the response so a client can correlate a support
    report with server-side logs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "").strip()
        request_id = incoming[:64] if incoming else uuid.uuid4().hex[:16]
        request.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response


# Legacy templates still contain a small set of static English labels.  This
# middleware translates only exact text nodes in the HTML response, before it
# reaches the browser; it replaces the old browser-side bangla-ui.js layer.
# New/edited templates should continue to use Django {% translate %} tags.
_BENGALI_STATIC_COPY = {
    "Stocks": "শেয়ারসমূহ", "All exchanges": "সব এক্সচেঞ্জ", "All actions": "সব সংকেত", "Filter": "ফিল্টার",
    "Regularly traded shares": "নিয়মিত লেনদেন হওয়া শেয়ার", "Limited data / low liquidity shares": "সীমিত ডেটা / কম লিকুইডিটি শেয়ার",
    "Code": "কোড", "Company": "কোম্পানি", "Price": "দাম", "Action": "সংকেত", "Score": "স্কোর", "Mature": "পরিণত", "Peak": "শীর্ষ", "Signal": "সংকেত",
    "Watchlist": "ওয়াচলিস্ট", "Your watchlist": "আপনার ওয়াচলিস্ট", "Remove": "সরান", "Disabled": "বন্ধ",
    "Portfolio": "পোর্টফোলিও", "Portfolios": "পোর্টফোলিওসমূহ", "Your portfolios": "আপনার পোর্টফোলিওসমূহ", "Default": "ডিফল্ট", "Open": "খুলুন",
    "Current value": "বর্তমান মূল্য", "Gain/loss": "লাভ/ক্ষতি", "Create a new portfolio": "নতুন পোর্টফোলিও তৈরি করুন", "Create portfolio": "পোর্টফোলিও তৈরি করুন",
    "Holdings": "হোল্ডিংস", "Add holding": "হোল্ডিং যোগ করুন", "Add transaction": "লেনদেন যোগ করুন", "Export CSV": "CSV এক্সপোর্ট", "Refresh": "রিফ্রেশ",
    "Goal tracker": "লক্ষ্য ট্র্যাকার", "Risk snapshot": "ঝুঁকির সারাংশ", "Invested (cost basis)": "বিনিয়োগ (ক্রয়মূল্য)", "Total gain/loss": "মোট লাভ/ক্ষতি",
    "Today's gain/loss": "আজকের লাভ/ক্ষতি", "Open holdings": "খোলা হোল্ডিংস", "Best performer": "সেরা পারফর্মার", "Worst performer": "সবচেয়ে দুর্বল পারফর্মার",
    "Alerts": "সতর্কতা", "Alerts & digests": "সতর্কতা ও সারাংশ", "New personal alert": "নতুন ব্যক্তিগত সতর্কতা", "My rules": "আমার নিয়মসমূহ",
    "Target price": "লক্ষ্য মূল্য", "Daily move %": "দৈনিক পরিবর্তন %", "Minimum confidence %": "ন্যূনতম আস্থা %", "Show in app": "অ্যাপে দেখান", "Send to Telegram": "টেলিগ্রামে পাঠান",
    "Create alert": "সতর্কতা তৈরি করুন", "Active": "সক্রিয়", "Paused": "স্থগিত", "Pause": "স্থগিত করুন", "Enable": "সক্রিয় করুন", "Delete": "মুছুন", "Delivered alerts": "পাঠানো সতর্কতা",
    "Backtests": "ব্যাকটেস্ট", "Trades": "লেনদেন", "Win rate": "জয়ের হার", "Avg return": "গড় রিটার্ন", "Avg peak days": "শীর্ষে পৌঁছানোর গড় দিন",
    "Compare stocks": "শেয়ার তুলনা", "Compare selected": "নির্বাচিত শেয়ার তুলনা করুন", "Stock": "শেয়ার", "Last price": "সর্বশেষ মূল্য", "Day move": "দৈনিক পরিবর্তন", "Confidence": "আস্থা", "Risk": "ঝুঁকি",
    "Profile": "প্রোফাইল", "Notification settings": "নোটিফিকেশন সেটিংস", "Email alerts": "ইমেইল সতর্কতা", "Save": "সংরক্ষণ করুন",
    "My feedback": "আমার মতামত", "Submit feedback": "মতামত পাঠান", "Reference": "রেফারেন্স", "Title": "শিরোনাম", "Category": "বিভাগ", "Status": "অবস্থা", "Submitted": "জমা দেওয়া", "Prev": "পূর্ববর্তী", "Next": "পরবর্তী",
    "Price overview": "মূল্যের সারাংশ", "Predict": "অনুমান করুন", "Technicals": "টেকনিক্যালস", "Why this estimate?": "এই অনুমানের কারণ কী?", "Predictive estimate": "পূর্বাভাসমূলক অনুমান", "Stale data": "পুরোনো ডেটা",
    "Experimental research candidate": "পরীক্ষামূলক গবেষণার শেয়ার", "No demonstrated predictive edge": "প্রমাণিত পূর্বাভাসের সুবিধা নেই",
    "Autonomous Paper Trading": "স্বয়ংক্রিয় পেপার ট্রেডিং", "Admin only · simulation": "শুধু অ্যাডমিন · সিমুলেশন", "Running": "চলমান",
    "Pause automation": "স্বয়ংক্রিয়তা স্থগিত করুন", "Start automation": "স্বয়ংক্রিয়তা চালু করুন", "Run virtual cycle now": "এখন ভার্চুয়াল সাইকেল চালান",
    "Starting cash": "শুরুর নগদ", "Available cash": "উপলভ্য নগদ", "Unsettled sale proceeds": "অমীমাংসিত বিক্রয় আয়", "Holdings value": "হোল্ডিংসের মূল্য", "Total equity": "মোট ইকুইটি", "Total return": "মোট রিটার্ন",
    "Open positions": "খোলা পজিশন", "Opened": "খোলা হয়েছে", "Matures": "মেয়াদ শেষ", "Quantity": "পরিমাণ", "Entry": "প্রবেশ মূল্য", "Current": "বর্তমান", "Unrealized P/L": "অবাস্তবায়িত লাভ/ক্ষতি",
    "Portfolio performance": "পোর্টফোলিও পারফরম্যান্স", "Virtual trade log": "ভার্চুয়াল লেনদেন তালিকা", "Date": "তারিখ", "Side": "ধরন", "Execution": "এক্সিকিউশন", "Fee": "ফি", "Reason": "কারণ",
    "Strategy guardrails": "কৌশলগত সুরক্ষা নিয়ম", "Evidence, not an edge claim": "প্রমাণ, লাভের নিশ্চয়তা নয়", "Learning feedback": "শেখার ফলাফল", "What the paper trades are teaching us": "পেপার ট্রেড থেকে আমরা কী শিখছি",
    "No real money or broker transactions.": "কোনো আসল অর্থ বা ব্রোকার লেনদেন নয়।", "No virtual trades have been executed.": "এখনও কোনো ভার্চুয়াল লেনদেন হয়নি।", "The chart will appear after at least two daily closing snapshots.": "কমপক্ষে দুই দিনের ক্লোজিং স্ন্যাপশটের পর চার্ট দেখা যাবে।",
}
_BENGALI_STATIC_PATTERN = re.compile(
    r"(?<=>)(\s*)(" + "|".join(re.escape(key) for key in sorted(_BENGALI_STATIC_COPY, key=len, reverse=True)) + r")(\s*)(?=<)"
)


class BengaliStaticCopyMiddleware:
    """Render remaining static Bengali labels on the server, never in JS."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            getattr(request, "LANGUAGE_CODE", "") != "bn"
            or not response.get("Content-Type", "").startswith("text/html")
            or getattr(response, "streaming", False)
        ):
            return response
        html = response.content.decode(response.charset or "utf-8")
        html = _BENGALI_STATIC_PATTERN.sub(lambda match: f"{match.group(1)}{_BENGALI_STATIC_COPY[match.group(2)]}{match.group(3)}", html)
        response.content = html.encode(response.charset or "utf-8")
        return response

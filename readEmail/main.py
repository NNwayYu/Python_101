import re # regular expressions
import win32com.client # for Windows COM (Component Object Model) automation
from transformers import pipeline # Hugging Face transformers

# ----------------------------
# SETTINGS
# ----------------------------
SUBJECT_KEYWORD = "Event"
MAX_EMAILS = 10

MODEL_NAME = "facebook/bart-large-cnn" # summarization model
CHUNK_WORDS = 350
MIN_SUM_LEN = 60
MAX_SUM_LEN = 180

SEND_TO = ""   # <-- add email recipient here
SEND_CC = ""                     # optional
DIGEST_SUBJECT = f"Email summaries (subject contains '{SUBJECT_KEYWORD}')"

# If True -> sends immediately. If False -> opens draft for you to review.
SEND_IMMEDIATELY = False


# ----------------------------
# OUTLOOK HELPERS
# ----------------------------
def get_outlook_namespace():
    outlook = win32com.client.Dispatch("Outlook.Application")
    return outlook, outlook.GetNamespace("MAPI")

def get_inbox_items(namespace):
    inbox = namespace.GetDefaultFolder(6)  # 6 = Inbox
    items = inbox.Items
    items.Sort("[ReceivedTime]", True)
    return items

def restrict_subject_contains(items, keyword: str):
    filter_query = f"""@SQL=(
    "urn:schemas:httpmail:subject" = '{keyword}'
    OR "urn:schemas:httpmail:subject" LIKE '{keyword} %'
    OR "urn:schemas:httpmail:subject" LIKE '% {keyword}'
    OR "urn:schemas:httpmail:subject" LIKE '% {keyword} %'
)"""


    return items.Restrict(filter_query)


# ----------------------------
# CLEANUP / CHUNKING
# ----------------------------
def clean_email_body(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    stop_patterns = [
        r"\n-{2,}\s*Original Message\s*-{2,}\n",
        r"\n-{2,}\s*Forwarded message\s*-{2,}\n",
        r"\nFrom:\s.*\nSent:\s.*\nTo:\s.*\nSubject:\s.*\n",
        r"\nOn .* wrote:\n",
    ]
    for pat in stop_patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            text = text[:m.start()]
            break

    sig_markers = [
        "\nBest regards",
        "\nKind regards",
        "\nRegards",
        "\nThanks,",
        "\nThank you,",
        "\nSincerely,",
    ]
    lower = text.lower()
    for marker in sig_markers:
        idx = lower.find(marker.lower())
        if idx != -1 and idx > 50:
            text = text[:idx]
            break

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

def chunk_by_words(text: str, words_per_chunk: int):
    words = text.split()
    return [" ".join(words[i:i + words_per_chunk]) for i in range(0, len(words), words_per_chunk)]


# ----------------------------
# SUMMARIZATION
# ----------------------------
def summarize_text(summarizer, text: str) -> str:
    text = clean_email_body(text)
    if not text:
        return ""

    chunks = chunk_by_words(text, CHUNK_WORDS)

    if len(chunks) == 1:
        out = summarizer(text, max_length=MAX_SUM_LEN, min_length=MIN_SUM_LEN, do_sample=False)
        return out[0]["summary_text"].strip()

    partial = []
    for ch in chunks:
        out = summarizer(ch, max_length=MAX_SUM_LEN, min_length=MIN_SUM_LEN, do_sample=False)
        partial.append(out[0]["summary_text"].strip())

    combined = " ".join(partial)
    out2 = summarizer(combined, max_length=MAX_SUM_LEN, min_length=MIN_SUM_LEN, do_sample=False)
    return out2[0]["summary_text"].strip()


# ----------------------------
# EMAIL SENDING
# ----------------------------
def build_digest_body(summaries):
    """
    Plain-text digest body.
    """
    lines = []
    lines.append("Hello,")
    lines.append("")
    lines.append(f"Here are summaries for the latest emails where subject contains '{SUBJECT_KEYWORD}':")
    lines.append("")

    for i, s in enumerate(summaries, start=1):
        lines.append(f"{i}) Subject: {s['subject']}")
        lines.append(f"   From: {s['from']}")
        lines.append(f"   Received: {s['received']}")
        lines.append("   Summary:")
        # indent summary nicely
        for ln in s["summary"].splitlines():
            lines.append(f"     - {ln}".rstrip() if ln.strip() else "     ")
        lines.append("")

    lines.append("Regards,")
    lines.append("Automated Summary Bot")
    return "\n".join(lines)

def send_outlook_email(outlook_app, to_addr, cc_addr, subject, body_text, send_now=False):
    mail = outlook_app.CreateItem(0)  # 0 = olMailItem
    mail.To = to_addr
    if cc_addr:
        mail.CC = cc_addr
    mail.Subject = subject
    mail.Body = body_text  # plain text

    if send_now:
        mail.Send()
    else:
        mail.Display()  # open draft for review


# ----------------------------
# MAIN
# ----------------------------
def main():
    print(f"Loading summarization model: {MODEL_NAME} ...")
    summarizer = pipeline("summarization", model=MODEL_NAME)

    outlook_app, namespace = get_outlook_namespace()
    items = get_inbox_items(namespace)
    filtered = restrict_subject_contains(items, SUBJECT_KEYWORD)

    summaries = []
    count = 0

    for msg in filtered:
        if MAX_EMAILS is not None and count >= MAX_EMAILS:
            break

        try:
            subject = getattr(msg, "Subject", "") or ""
            sender = getattr(msg, "SenderName", "") or ""
            received = getattr(msg, "ReceivedTime", "") or ""
            body = getattr(msg, "Body", "") or ""

            summary = summarize_text(summarizer, body) or "(No content to summarize)"

            summaries.append({
                "subject": subject,
                "from": sender,
                "received": received,
                "summary": summary
            })

            count += 1
        except Exception as e:
            # Skip non-mail items or errors
            continue

    if not summaries:
        print("No matching emails found. Nothing to send.")
        return

    digest_body = build_digest_body(summaries)

    send_outlook_email(
        outlook_app=outlook_app,
        to_addr=SEND_TO,
        cc_addr=SEND_CC,
        subject=DIGEST_SUBJECT,
        body_text=digest_body,
        send_now=SEND_IMMEDIATELY
    )

    print(f"Prepared digest for {len(summaries)} emails.")
    print("Sent immediately." if SEND_IMMEDIATELY else "Opened draft (review then click Send).")


if __name__ == "__main__":
    main()

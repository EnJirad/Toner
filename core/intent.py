"""Intent routing for natural-language messages.

Classifies a user message into exactly one intent category using a hybrid
strategy:

1. A deterministic fast path for greetings, pleasantries and follow-ups.
2. Model-based classification when a model is available (this is what makes
   natural conversation work for requests the heuristics cannot know about).
3. A keyword heuristic fallback when no model is configured, the call fails,
   or the model returned an unusable answer.

The router is pure logic: it never touches the terminal, the filesystem or
tools, so it is independent from the CLI and unit testable in isolation.

Categories:
    CHAT           greetings, small talk, brainstorming, follow-up discussion
    QUESTION       questions about code, errors, architecture or behaviour
    INSPECT        inspect/diagnose the project without changing files
    PLAN           propose an implementation plan without changing files
    CODE_TASK      create, edit, fix, refactor or otherwise change code
    TEST           run tests/builds/linting or other verification
    GIT_TASK       inspect Git state or perform an explicitly asked Git action
    CLARIFICATION  genuinely ambiguous; ask one concise question
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from core.textutil import extract_json_object, truncate_middle

DEFAULT_CLARIFICATION = (
    "I'm not sure what you'd like me to do. Should I explain something, "
    "inspect the project, plan a change, or start coding?"
)


class Intent(str, Enum):
    CHAT = "CHAT"
    QUESTION = "QUESTION"
    INSPECT = "INSPECT"
    PLAN = "PLAN"
    CODE_TASK = "CODE_TASK"
    TEST = "TEST"
    GIT_TASK = "GIT_TASK"
    CLARIFICATION = "CLARIFICATION"


VALID_INTENTS = {intent.value for intent in Intent}


@dataclass
class IntentResult:
    intent: str
    confidence: float = 1.0
    source: str = "heuristic"
    reason: str = ""
    follow_up: bool = False
    clarification_question: str = ""
    scores: dict = field(default_factory=dict)

    @property
    def needs_clarification(self):
        return self.intent == Intent.CLARIFICATION.value


# -- fast path patterns ---------------------------------------------------

# A message is treated as a greeting only when it matches one of these AND
# carries no actionable content, so "hello, fix the login bug" still codes.
GREETING_RE = re.compile(
    r"^(?:"
    r"hi|hello|hey|hiya|yo|howdy|hallo|halo|"
    r"good\s+(?:morning|afternoon|evening|day)|"
    r"how\s+are\s+you|how'?s\s+it\s+going|how\s+do\s+you\s+do|"
    r"สวัสดี(?:ครับ|ค่ะ|นะ)?|หวัดดี|เป็นไงบ้าง|เป็นยังไงบ้าง|ยังไงบ้าง|"
    r"สบายดี(?:ไหม|นะ)?"
    r")[\s.!?]*$",
    re.IGNORECASE,
)

PLEASANTRY_RE = re.compile(
    r"^(?:"
    r"thanks|thank\s+you|thx|ok(?:ay)?|cool|nice|great|good|perfect|"
    r"bye|bye[\s-]bye|see\s+you|got\s+it|understood|"
    r"ขอบคุณ(?:ครับ|ค่ะ|มาก)?|โอเค|เยี่ยม|ดีมาก|รับทราบ|ลาก่อน|บาย"
    r")[\s.!?]*$",
    re.IGNORECASE,
)

FOLLOW_TEST_RE = re.compile(
    r"^(?:"
    r"tests?\s+again|re-?run(?:\s+the\s+tests?)?|run\s+(?:the\s+)?tests?\s+again|"
    r"test\s+again|try\s+(?:it|that|them)\s+again|run\s+tests?\s+one\s+more\s+time|"
    r"ทดสอบอีกครั้ง|รันอีกครั้ง|รันเทสต์อีกครั้ง|รันทดสอบอีกครั้ง|"
    r"ลองใหม่|ทดสอบใหม่|รันใหม่"
    r")",
    re.IGNORECASE,
)

FOLLOW_CONTINUE_RE = re.compile(
    r"^(?:"
    r"continue|go\s+on|keep\s+going|carry\s+on|resume|proceed|"
    r"ทำต่อ|ทำต่อเลย|แก้ต่อ|แก้ต่อเลย|เขียนต่อ|ต่อเลย|ไปต่อ|เดินหน้าต่อ|ลุยต่อ"
    r")",
    re.IGNORECASE,
)

# -- heuristic keyword tables --------------------------------------------

# English keywords are matched on word boundaries; non-ASCII (Thai) keywords
# are matched as substrings because Thai has no whitespace word separation.
_KEYWORDS = {
    Intent.QUESTION.value: [
        "why",
        "what",
        "how",
        "where",
        "which",
        "who",
        "explain",
        "describe",
        "meaning",
        "meaning of",
        "does",
        "is it",
        "can you tell",
        "whats",
        "what's",
        "ทำไม",
        "อะไร",
        "ยังไง",
        "อย่างไร",
        "หมายความ",
        "อธิบาย",
        "คืออะไร",
        "ได้อย่างไร",
        "ทำไม่ได้",
    ],
    Intent.CODE_TASK.value: [
        "fix",
        "implement",
        "add",
        "create",
        "edit",
        "refactor",
        "update",
        "patch",
        "change",
        "write",
        "delete",
        "remove",
        "rename",
        "move",
        "build the",
        "make the",
        "bug in",
        "เขียน",
        "แก้ไข",
        "แก้บั๊ก",
        "แก้บั๊กนี้",
        "สร้าง",
        "เพิ่ม",
        "ลบ",
        "ย้าย",
        "เปลี่ยน",
        "ปรับปรุง",
        "ทำฟีเจอร์",
        "แก้ให้",
        "ซ่อม",
    ],
    Intent.INSPECT.value: [
        "inspect",
        "diagnose",
        "diagnosis",
        "check the project",
        "check the code",
        "review the project",
        "audit",
        "scan",
        "look into",
        "look at",
        "investigate",
        "overview",
        "what needs fixing",
        "what's wrong",
        "whats wrong",
        "ตรวจสอบ",
        "ตรวจดู",
        "ตรวจเช็ก",
        "ดูโปรเจกต์",
        "ดูโค้ด",
        "ดูให้หน่อย",
        "สแกน",
        "วิเคราะห์",
        "หาบั๊ก",
    ],
    Intent.PLAN.value: [
        "plan",
        "planning",
        "propose",
        "proposal",
        "outline",
        "design approach",
        "steps to",
        "roadmap",
        "วางเ plans",
        "วางแผน",
        "วางแผนให้",
        "เสนอแนวทาง",
        "ร่างแผน",
        "วางแผนงาน",
    ],
    Intent.TEST.value: [
        "run the tests",
        "run tests",
        "run test",
        "run the test",
        "tests again",
        "re-run",
        "rerun",
        "pytest",
        "unittest",
        "lint",
        "linter",
        "typecheck",
        "type-check",
        "coverage",
        "make sure the tests",
        "รันเทสต์",
        "รันทดสอบ",
        "รันทดสอบ",
        "รัน test",
        "ทดสอบอีก",
        "รัน pytest",
        "รัน lint",
    ],
    Intent.GIT_TASK.value: [
        "git status",
        "git diff",
        "git log",
        "git branch",
        "git commit",
        "git checkout",
        "commit the",
        "commit this",
        "push to",
        "merge branch",
        "show the diff",
        "show the commits",
        "สถานะ git",
        "คอมมิต",
        "ฝาก commit",
        "ดู commit",
    ],
    Intent.CHAT.value: [
        "brainstorm",
        "what do you think",
        "talk about",
        "discuss",
        "คิดยังไง",
        "ระดมสมอง",
        "คุย",
        "ถามเฉยๆ",
    ],
}

_QUESTION_FIRST_TOKENS = {
    "why",
    "what",
    "how",
    "which",
    "who",
    "where",
    "when",
    "is",
    "are",
    "do",
    "does",
    "can",
    "could",
    "should",
    "would",
    "whats",
    "ทำไม",
    "อะไร",
    "ยังไง",
    "อย่างไร",
    "คืออะไร",
    "หมายความ",
    "เมื่อไหร่",
    "ที่ไหน",
    "อธิบาย",
}

# Tie-break preference when two categories score the same. The first match
# in this order wins, unless the message looks like a question (see below).
_TIE_ORDER = [
    Intent.CODE_TASK.value,
    Intent.TEST.value,
    Intent.GIT_TASK.value,
    Intent.INSPECT.value,
    Intent.PLAN.value,
    Intent.QUESTION.value,
    Intent.CHAT.value,
]


def _keyword_matches(keyword, lowered, text):
    if keyword.isascii():
        return bool(re.search(rf"\b{re.escape(keyword)}\b", lowered))
    return keyword in text


class IntentRouter:
    """Classify natural-language messages into :class:`Intent` categories."""

    def __init__(self, settings=None, model_router=None, redactor=None):
        self.settings = dict(settings or {})
        self.model_router = model_router
        self.redactor = redactor
        self.model_enabled = bool(
            self.settings.get("api_key") or self.settings.get("model")
        )

    # -- public ----------------------------------------------------------

    def classify(self, text, active_task=None, history=None):
        """Classify ``text``. Never raises."""
        raw = str(text or "").strip()

        if not raw:
            return IntentResult(
                Intent.CLARIFICATION.value,
                confidence=1.0,
                source="fast_path",
                reason="empty message",
                clarification_question="What would you like me to do?",
            )

        follow = self._follow_up(raw, active_task)

        if follow is not None:
            return follow

        heuristic = self._heuristic(raw)

        if self._is_greeting(raw, heuristic):
            return IntentResult(
                Intent.CHAT.value,
                confidence=1.0,
                source="fast_path",
                reason="greeting or pleasantry",
            )

        model = self._model_classify(raw, active_task, history)

        if model is not None:
            return model

        return heuristic

    # -- fast path -------------------------------------------------------

    def _follow_up(self, text, active_task):
        if FOLLOW_TEST_RE.match(text):
            return IntentResult(
                Intent.TEST.value,
                confidence=0.95,
                source="follow_up",
                reason="repeat verification request",
                follow_up=True,
            )

        if FOLLOW_CONTINUE_RE.match(text):
            if active_task:
                return IntentResult(
                    Intent.CODE_TASK.value,
                    confidence=0.95,
                    source="follow_up",
                    reason="continue the active task",
                    follow_up=True,
                )

            return IntentResult(
                Intent.CODE_TASK.value,
                confidence=0.9,
                source="follow_up",
                reason="continue requested with no active task yet",
                follow_up=True,
            )

        return None

    def _is_greeting(self, text, heuristic):
        # Actionable content wins over "hello, fix the login bug": the message
        # may only be a greeting when the heuristic found no real request.
        if heuristic.intent not in (Intent.CHAT.value, Intent.CLARIFICATION.value):
            return False

        return bool(GREETING_RE.match(text) or PLEASANTRY_RE.match(text))

    # -- model classification --------------------------------------------

    def _model_classify(self, text, active_task, history):
        if self.model_router is None or not self.model_enabled:
            return None

        context_lines = []

        for message in list(history or [])[-6:]:
            role = message.get("role", "user")
            content = str(message.get("content") or "")
            if self.redactor is not None:
                content = self.redactor.redact(content)
            context_lines.append(f"{role}: {truncate_middle(content, 400)}")

        system = (
            "You are an intent classifier for an AI coding agent named Toner.\n"
            "Classify the LATEST user message into exactly one of:\n"
            "CHAT, QUESTION, INSPECT, PLAN, CODE_TASK, TEST, GIT_TASK, CLARIFICATION\n\n"
            "Rules:\n"
            "- Greetings, small talk, thanks and general discussion are CHAT.\n"
            "- Questions about code, errors or behaviour are QUESTION.\n"
            "- Requests to inspect or diagnose without changing files are INSPECT.\n"
            "- Requests to propose a plan are PLAN.\n"
            "- Requests to create, edit, fix or refactor code are CODE_TASK.\n"
            "- Requests to run tests, builds or linting are TEST.\n"
            "- Requests about Git state or explicitly requested Git operations are GIT_TASK.\n"
            "- Genuinely ambiguous messages that lack essential information are CLARIFICATION.\n"
            "- Never classify a message as CODE_TASK just because it is short.\n\n"
            'Reply with ONLY a JSON object: {"intent": "...", "confidence": 0.0-1.0, '
            '"reason": "...", "clarification_question": "only when CLARIFICATION"}'
        )

        parts = []

        if active_task:
            parts.append(f"Active task: {active_task}")

        if context_lines:
            parts.append("Recent conversation:\n" + "\n".join(context_lines))

        parts.append(f"Latest user message:\n{text}")

        try:
            message = self.model_router.chat(
                "router",
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "\n\n".join(parts)},
                ],
                temperature=0.0,
            )
        except Exception:  # classification must never crash the CLI
            return None

        content = message.get("content") if isinstance(message, dict) else None

        if not content:
            return None

        try:
            data = extract_json_object(str(content))
        except ValueError:
            return None

        intent = str(data.get("intent") or "").strip().upper()

        if intent not in VALID_INTENTS:
            return None

        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5

        confidence = max(0.0, min(1.0, confidence))
        question = str(data.get("clarification_question") or "").strip()

        if intent == Intent.CLARIFICATION.value and not question:
            question = DEFAULT_CLARIFICATION

        return IntentResult(
            intent,
            confidence=confidence,
            source="model",
            reason=str(data.get("reason") or "")[:300],
            clarification_question=question,
        )

    # -- heuristic fallback ----------------------------------------------

    def _heuristic(self, text):
        if not re.search(r"[0-9A-Za-z\u0e00-\u0e7f]", text):
            # Punctuation or symbols alone carry no intent.
            return IntentResult(
                Intent.CLARIFICATION.value,
                confidence=0.3,
                source="heuristic",
                reason="no word content",
                clarification_question=DEFAULT_CLARIFICATION,
                scores={},
            )

        lowered = text.lower()
        scores = {}

        for intent, keywords in _KEYWORDS.items():
            score = 0
            matched = 0

            for keyword in keywords:
                if _keyword_matches(keyword, lowered, text):
                    matched += 1

            if matched:
                score = min(4, 1 + matched)
                scores[intent] = score

        if text.rstrip().endswith("?"):
            scores[Intent.QUESTION.value] = scores.get(Intent.QUESTION.value, 0) + 1

        # An explicit leading plan verb ("วางแผน...", "plan the...") makes the
        # request a plan even when it also contains implementation words.
        for marker in ("วางแผน", "plan ", "plan the", "proposal", "propose", "outline"):
            if lowered.startswith(marker):
                scores[Intent.PLAN.value] = scores.get(Intent.PLAN.value, 0) + 3
                break

        if not scores:
            return IntentResult(
                Intent.CLARIFICATION.value,
                confidence=0.3,
                source="heuristic",
                reason="no recognised intent",
                clarification_question=DEFAULT_CLARIFICATION,
                scores={},
            )

        best = max(scores.values())
        top = [intent for intent, score in scores.items() if score == best]
        first_token = re.split(r"[\s.,!?¿]+", lowered, maxsplit=1)[0]
        looks_like_question = (
            text.rstrip().endswith("?") or first_token in _QUESTION_FIRST_TOKENS
        )

        if len(top) > 1:
            if looks_like_question and Intent.QUESTION.value in top:
                choice = Intent.QUESTION.value
            else:
                choice = next(
                    intent for intent in _TIE_ORDER if intent in top
                )
        else:
            choice = top[0]

        return IntentResult(
            choice,
            confidence=min(0.9, 0.5 + 0.1 * best),
            source="heuristic",
            reason=f"keyword score {scores.get(choice)}",
            scores=scores,
        )

"""Gap flags: what a context card is missing, and how the page says it.

A missing source never fails a run; it becomes one of these flags on the card.
"""
from enum import Enum


class Gap(str, Enum):
    NO_CALL_LOGGED = "no_call_logged"
    NO_ICP = "no_icp"
    NO_CONTACT = "no_contact"
    NO_SETU_MATCH = "no_setu_match"
    NO_MAIL = "no_mail"
    UNVERIFIED = "unverified"


GAP_COPY: dict[Gap, str] = {
    Gap.NO_CALL_LOGGED: "No call logged.",
    Gap.NO_ICP: "No account fit read for this company yet.",
    Gap.NO_CONTACT: "No contact on the Zoho record.",
    Gap.NO_SETU_MATCH: "No Setu case study matched.",
    Gap.NO_MAIL: "No mail found.",
    Gap.UNVERIFIED: "Some facts could not be verified.",
}


def gap_copy(flag: str) -> str:
    try:
        return GAP_COPY[Gap(flag)]
    except ValueError:
        return flag.replace("_", " ").capitalize() + "."

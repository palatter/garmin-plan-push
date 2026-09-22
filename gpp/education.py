"""What each kind of session does, and why (#164, #200).

Athletica's one real differentiator in the 2026 comparisons was explaining
the reasoning behind the training. These are the explanations a coach gives
on the first day, with the evidence line that backs each one, shown in the
app beside the session and printed by `gpp why <topic>`.
"""

from __future__ import annotations

ENTRIES: dict[str, dict[str, str]] = {
    "easy": {
        "title": "Easy run",
        "what": "Aerobic volume at a conversational effort. Most of the week's running, and the part "
        "that builds the capillaries, mitochondria and tendon tolerance everything else stands on.",
        "feel": "You can speak in full sentences. If you cannot, it is not easy.",
        "evidence": "Across 17 studies and 437 athletes, mostly-easy distributions (around 80% of "
        "time) outperform mixed ones; the exact split matters less than keeping the easy days easy.",
        "source": "Oliveira et al., Sports Medicine 2024 (meta-analysis)",
    },
    "recovery": {
        "title": "Recovery run",
        "what": "A short, very easy run the day after hard work: circulation and habit, not fitness.",
        "feel": "Embarrassingly slow. Heart rate stays in zone 1.",
        "evidence": "Foster's monotony index: the same load every day, not high load, precedes "
        "overtraining -- the recovery day is what makes the hard day possible.",
        "source": "Foster, Medicine & Science in Sports & Exercise 1998",
    },
    "steady": {
        "title": "Steady run",
        "what": "The middle gear: quicker than easy, well below threshold. The band naive plans skip.",
        "feel": "Purposeful; a sentence at a time, not a paragraph.",
        "evidence": "Plans with no moderate work are the pattern runners describe as 'mild or brutal'; "
        "pyramidal distributions that keep this band outperform polarized ones in recreational runners.",
        "source": "Rosenblatt et al. 2025; PMC10915606 (coach evaluation of AI plans)",
    },
    "marathon": {
        "title": "Marathon-pace run",
        "what": "Sustained running at goal marathon pace, usually inside a long run, to make the pace "
        "familiar and to practise fuelling at it.",
        "feel": "Comfortably hard for an hour; you could hold it for the race, not forever.",
        "evidence": "Physiological resilience -- how little you slow down late in a race -- is trained "
        "by long runs with race-pace work and fuelling practice, not by intervals alone.",
        "source": "Jones, Scandinavian Journal of Medicine & Science in Sports 2025",
    },
    "threshold": {
        "title": "Threshold session",
        "what": "Cruise intervals or a tempo run around lactate threshold: the pace you could hold for "
        "about an hour. Raises the speed you can sustain without accumulating fatigue.",
        "feel": "Comfortably hard. Controlled, never a race; you should finish wanting one more rep.",
        "evidence": "Threshold work is the backbone of every distance-running system in the literature, "
        "from Daniels' T-pace to the Norwegian sub-threshold approach.",
        "source": "Daniels, Daniels' Running Formula; Casado et al. 2023",
    },
    "interval": {
        "title": "VO2max intervals",
        "what": "Repeats of two to five minutes at about 3k-5k race pace with jogged recoveries, to "
        "raise the ceiling on oxygen delivery.",
        "feel": "Hard. Breathing is the limit; the last rep should be as fast as the first.",
        "evidence": "Intervals at 90-100% of VO2max produce the largest VO2max gains per session; "
        "more than 8-10% of weekly time here is where injury and staleness start.",
        "source": "Buchheit & Laursen, Sports Medicine 2013",
    },
    "repetition": {
        "title": "Repetition work",
        "what": "Short, fast reps -- 200 to 400 m at mile pace -- with full recovery, for speed and "
        "economy, not for fitness.",
        "feel": "Fast and relaxed; if the recovery feels too long, it is right.",
        "evidence": "Running economy improves with high-speed work and with heavy strength training, "
        "and economy is what separates runners of equal VO2max.",
        "source": "Daniels; Llanos-Lagos et al. 2024 (meta-analysis on economy)",
    },
    "strides": {
        "title": "Strides",
        "what": "Four to eight 20-second accelerations after an easy run, walking back between them.",
        "feel": "Smooth and tall, building to near-sprint, never straining.",
        "evidence": "The cheapest way to keep leg speed and mechanics through a base phase; every "
        "Daniels plan carries them.",
        "source": "Daniels' Running Formula",
    },
    "hills": {
        "title": "Hill repeats",
        "what": "Short hard efforts uphill with a jog down. Strength and power without the impact of "
        "flat speedwork.",
        "feel": "Drive the knees, short quick steps, arms working.",
        "evidence": "Hill and downhill running trains the fatigue resistance late-race performance "
        "depends on; uphill work loads the muscles with lower impact forces than track reps.",
        "source": "Jones 2025 (resilience); Lydiard's hill phase",
    },
    "long": {
        "title": "Long run",
        "what": "The week's longest run, mostly easy, sometimes with a faster finish. Endurance, "
        "fuelling practice and time on feet.",
        "feel": "Easy for most of it; the last third is where it should start to feel like work.",
        "evidence": "A single run more than ~10% longer than anything in the previous 30 days raised "
        "overuse-injury risk in a 5,200-runner cohort -- grow it, never jump it.",
        "source": "Damsted et al. / Nielsen et al. (RUNCLEVER cohort)",
    },
    "medium-long": {
        "title": "Medium-long run",
        "what": "Ninety minutes to two hours midweek. The second endurance stimulus of a marathon "
        "week, and the one most plans leave out.",
        "feel": "Like a long run's first half.",
        "evidence": "Pfitzinger's marathon plans are built around two long efforts a week; the "
        "midweek one is what makes the volume marathon-specific.",
        "source": "Pfitzinger & Douglas, Advanced Marathoning",
    },
    "race": {
        "title": "Race day",
        "what": "The session everything else was for. Even pacing, a controlled start, fuel from the "
        "first 20-30 minutes in anything over an hour.",
        "feel": "The first third feels too easy. That is correct.",
        "evidence": "In the largest split analysis to date, even pacing produced the fastest finishes at "
        "every level; recreational runners mostly go out too fast and positive-split.",
        "source": "PLOS One 2025 (marathon split analysis)",
    },
    "strength": {
        "title": "Strength session",
        "what": "Heavy lower-body lifts and plyometrics, two sessions a week, on easy days.",
        "feel": "Heavy and short; a strength session is not a workout for the lungs.",
        "evidence": "Strength training improves running economy at every speed and its durability under "
        "fatigue; two to three sessions a week, 48 hours apart, is the pattern in the trials.",
        "source": "Llanos-Lagos et al., Sports Medicine 2024; 2025 umbrella review",
    },
    "taper": {
        "title": "Taper",
        "what": "The final two weeks: volume down, intensity and frequency kept.",
        "feel": "Fresh and slightly restless. Resist the urge to test yourself.",
        "evidence": "Across 27 studies, cutting volume 41-60% over about two weeks while keeping "
        "intensity and frequency gave a ~2.2% performance gain; cutting more performed worse.",
        "source": "Bosquet et al., Medicine & Science in Sports & Exercise 2007 (meta-analysis)",
    },
    "rest": {
        "title": "Rest day",
        "what": "No running. Adaptation happens here, not during the session.",
        "feel": "Restless by day three of a taper; otherwise, fine.",
        "evidence": "Ten or more consecutive days of training without a day off is where monotony and "
        "sudden overuse injuries cluster.",
        "source": "Foster 1998; RUNCLEVER cohort",
    },
}

ALIASES = {
    "quality": "threshold",
    "tempo": "threshold",
    "t": "threshold",
    "e": "easy",
    "m": "marathon",
    "i": "interval",
    "vo2": "interval",
    "vo2max": "interval",
    "r": "repetition",
    "reps": "repetition",
    "stride": "strides",
    "hill": "hills",
    "cross": "easy",
    "cooldown": "easy",
    "warmup": "easy",
    "gym": "strength",
}


def lookup(topic: str) -> dict[str, str] | None:
    key = topic.strip().lower()
    key = ALIASES.get(key, key)
    return ENTRIES.get(key)


def topics_for(role: str | None, zones: list[str]) -> list[str]:
    """The entries worth showing for a session: its role, then its dominant zones."""
    out: list[str] = []
    if role and role in ENTRIES:
        out.append(role)
    elif role and ALIASES.get(role) in ENTRIES:
        out.append(ALIASES[role])
    for zone in zones:
        key = ALIASES.get(zone, zone)
        if key in ENTRIES and key not in out:
            out.append(key)
    return out[:2]


def render(entry: dict[str, str]) -> str:
    return (
        f"{entry['title']}\n\n{entry['what']}\n\nHow it should feel: {entry['feel']}\n\n"
        f"Evidence: {entry['evidence']}\n  -- {entry['source']}"
    )

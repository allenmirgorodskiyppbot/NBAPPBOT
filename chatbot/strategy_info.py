"""User-facing description of the implemented research benchmark."""


def strategy_summary():
    return (
        'Current strategy: 20-game baseline research.\n'
        '• Use the latest 20 usable, completed regular-season games for the player’s current team, '
        'strictly before the game being assessed. Earlier seasons are labeled; DNPs are not zeroes.\n'
        '• Compare the arithmetic average and median with the exact offered line. Small or conflicting '
        'differences produce a Pass in the stricter shortlist. Historical variation is descriptive, not a win probability.\n'
        '• Uploaded research choices can still show More/Less when the completed-game statistics are verified. '
        'Use the existing buffer first, then observed 20-game counts; break tied counts with the last five games, '
        'then the mean and median. Narrow support and recent contradictions are labeled. '
        'An expired live schedule check is a warning for this research, not a reason to hide all choices.\n'
        '• Check current game coverage, schedule, recent playing time and reported availability. '
        'Old opponent history and tentative defenders are context, not automatic bonuses.\n'
        '• Require 20 comparable games for a ranked pick. Shorter samples can still be discussed. '
        'Return fewer picks when needed; a complete lineup must meet your team and player constraints.\n'
        '• Save forecasts, offered lines and passes for future evaluation. Payouts remain unknown unless supplied.\n'
        'Validation: the earlier simulated 20-game rule scored 53.6%, versus 53.7% for Always Less. '
        'That simulation allowed shorter samples and used invented reference lines. '
        'It did not establish a profitable betting edge; the stricter live filters have not established one either.'
    )

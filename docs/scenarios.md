# Scenarios

## Purpose
This file describes realistic end-to-end scenarios demo and testing.

## Scenario 1: Normal dose confirmation
The user says they took their heart pill after breakfast. The assistant recognizes the medication, checks that no prior log exists for the active window, and records it as confirmed. The user hears a calm confirmation message.

## Scenario 2: Similar-looking pills
The user says they are taking the white pill. The assistant finds two white pills in the registry and asks a clarifying question using bottle clues or schedule clues. The system waits until the user responds before logging anything.

## Scenario 3: Forgotten dose uncertainty
The user says they cannot remember whether they took a dose. The assistant does not guess. It stores an uncertain event and tells the user not to take another dose until it is checked or a caregiver confirms.

## Scenario 4: Duplicate confirmation attempt
The user tries to confirm the same dose a second time within the active schedule window. The assistant blocks the action, explains that the dose already appears in the log, and keeps the original audit trail intact.

## Scenario 5: Unsafe medication combination
The user says they want to take an old pain pill with their daily blood thinner. The assistant immediately enters escalation flow, warns that the combination may be unsafe, and avoids normal conversational handling.

## Scenario 6: Caregiver correction
A caregiver notices the wrong pill was logged. The system requests explicit confirmation before changing the record and stores the correction as an auditable event.

## Scenario 7: Poor speech recognition
The speech-to-text layer mishears “heart pill” as “hair pill.” The assistant sees the confidence is too low, asks the user to repeat, and avoids a false confirmation.

## Scenario 8: Travel / timezone shift
The user is away from home and their schedule is affected by timezone changes. The assistant uses stored timezone data to avoid duplicate or missed dose mistakes.

## Scenario 9: Emergency overdose
The user says they accidentally took four pills. The assistant switches to emergency handling and does not continue a routine medication confirmation dialogue.

## Scenario 10: Supplement confusion
The user mentions vitamins or supplements with a prescription. The assistant should separate supplement mentions from prescription logging and avoid treating them as the same medication.

## Demo Priority
For the final demo, focus on:
1. Normal confirmation.
2. Ambiguous pill clarification.
3. Duplicate-dose blocking.
4. Unsafe medication escalation.
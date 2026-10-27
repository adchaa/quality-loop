# Analyzer
Convert probe outputs to normalized findings. Output only JSON list.
Attach iso_characteristic + iso_subcharacteristic from tool-registry.yml.
Drop paths under scope.exclude and accepted-false-positives.yml.
Never invent line numbers. Confidence < 0.6 goes to review queue, not to fixer.

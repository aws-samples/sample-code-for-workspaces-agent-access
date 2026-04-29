# Task: Extract Amazon Bedrock Text from PDF

## Goal
Copy the Amazon Bedrock description from an AWS PDF and save it to Desktop.

## PDF URL
```
https://docs.aws.amazon.com/pdfs/whitepapers/latest/aws-overview/aws-overview.pdf
```

## Steps

1. **Open Firefox**: Win key → type "Firefox" → Enter → wait 2 sec → Win+Up to maximize
2. **Load PDF**: Ctrl+L → Ctrl+A → paste URL above → Enter → wait 5 sec for PDF
3. **Find Bedrock**: Ctrl+F → type "Amazon Bedrock" → Enter → Escape
4. **Copy text**: Triple-click the Bedrock paragraph to select → Ctrl+C
5. **Open Writer**: Win key → type "OpenOffice Writer" → Enter
6. **Paste**: Click document area → Ctrl+V
7. **Save**: Ctrl+S → click Desktop → click filename field → Ctrl+A → type `aws-bedrock-overview` → click Save

## Done When
File `aws-bedrock-overview.txt` exists on Desktop with Bedrock description.

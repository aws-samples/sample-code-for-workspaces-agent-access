# Task: Extract Amazon Bedrock Text from PDF

Save the Amazon Bedrock description from an AWS PDF to a text file named `aws-bedrock-overview.txt`. Save location doesn't matter.

## PDF URL
```
https://docs.aws.amazon.com/pdfs/whitepapers/latest/aws-overview/aws-overview.pdf
```

## Plan (batch actions, minimize screenshots)

1. **Open Firefox + load PDF** (1 screenshot after PDF loads):
   - `key("super+r")` → `type_text("firefox")` → `key("Return")` → `wait(3)`
   - `key("ctrl+l")` → `key("ctrl+a")` → `type_text("<PDF URL>")` → `key("Return")` → `wait(5)`
   - `screenshot` to confirm PDF loaded

2. **Find Bedrock section** (1-2 screenshots to read text):
   - `key("ctrl+f")` → `type_text("Amazon Bedrock")` → `key("Return")` → `key("Escape")`
   - Use `key("F3")` to cycle through matches until you find the description section (not TOC)
   - `screenshot` to read the Bedrock description text

3. **Open Notepad + type text** (1 screenshot after typing):
   - `key("super+r")` → `type_text("notepad")` → `key("Return")` → `wait(1)`
   - `type_text("<the Bedrock description you read>")`
   - `screenshot` to verify text is in Notepad

4. **Save** (1 screenshot to confirm):
   - `key("ctrl+s")` → `wait(1)`
   - `key("ctrl+a")` → `type_text("aws-bedrock-overview")` → `key("Return")`
   - `screenshot` to confirm title bar shows the filename

## Target: Complete in under 8 screenshots and 40 tool calls.

## The Bedrock text to find (approximate)
"Amazon Bedrock is a fully managed service that makes foundation models (FMs) from Amazon and leading AI companies available through an API..."

## Done When
Notepad title bar shows `aws-bedrock-overview` confirming the file was saved.

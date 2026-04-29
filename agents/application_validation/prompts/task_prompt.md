# Task: Validate All Desktop Applications

Your task is to systematically test 9 Windows desktop applications to confirm they are installed, launchable, and functional.

## Applications to Test

1. Firefox
2. Notepad++
3. OpenOffice Calc (scalc)
4. OpenOffice Draw (sdraw)
5. OpenOffice Impress (simpress)
6. OpenOffice Math (smath)
7. OpenOffice Start Center (soffice)
8. OpenOffice Web (sweb)
9. OpenOffice Writer (swriter)

## Instructions

For **each application**, in the order listed above:

1. Press the Windows key to open the Start Menu
2. Type the application name and click the best matching result
3. Wait for the application to fully open
4. Dismiss any non-essential dialogs (update prompts, registration, file recovery dialogs)
5. Take a screenshot to record the opened state
6. Perform one basic interaction to confirm the app is responsive:
   - **Firefox**: Click the address bar and type `about:blank`, press Enter
   - **Notepad++**: Click the editor area and type `test`, then Ctrl+Z to undo
   - **OpenOffice Calc**: Click cell A1, type `123`, press Enter, then Ctrl+Z
   - **OpenOffice Draw**: Click on the canvas area
   - **OpenOffice Impress**: Click on the slide editing area
   - **OpenOffice Math**: Click the formula input area and type `a + b`
   - **OpenOffice Start Center**: Confirm the document type icons are visible
   - **OpenOffice Web**: Click on the main editing/viewing area
   - **OpenOffice Writer**: Click the document area and type `test`, then Ctrl+Z
7. Record whether the app is PASS, FAIL, or NOT FOUND
8. Close the application (Alt+F4 → Don't Save if prompted)
9. Confirm the window is closed before moving to the next application

## Expected Output

At the end of all testing, provide a complete validation report in this format:
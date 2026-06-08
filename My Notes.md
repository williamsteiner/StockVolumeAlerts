My Notes

the project is already set up correctly. The .venv folder is present and VS Code is opening the project root.

1. Open a terminal in VS Code

Press:

Terminal → New Terminal

or

Ctrl + ` (backtick)

You should see something like:

PS C:\Users\bill\StockVolumeAlerts>
2. Activate the virtual environment

In PowerShell:

.\.venv\Scripts\Activate.ps1

If successful, your prompt changes to:

(.venv) PS C:\Users\bill\StockVolumeAlerts>

If you get an execution policy error:

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

Then run the activate command again.

3. Install dependencies

If this is the first time running the project:

pip install -r requirements.txt

You should see packages being installed.

4. Check your config

Open:

config.json

Make sure your VTI settings and email settings are configured.

5. Run the program

From the project root:

python volume_alert.py

or

py volume_alert.py
6. If you want it to run continuously

Many of these alert scripts are designed to be run repeatedly. Depending on how Codex wrote it, either:

python volume_alert.py

runs once and exits,

or it stays running and checks periodically.

7. If it fails

Copy and paste the entire terminal output here. A common first test is:

python --version
pip --version
python volume_alert.py

and send me the results.

One thing I notice from your screenshot: you have both

config.json
config.json.example

which is correct, but make sure you actually edited config.json and not config.json.example. That's a very common mistake.


To exit the virtual environment, simply type:

deactivate

--------------
git remote add origin https://github.com/williamsteiner/StockVolumeAlerts.git
git push -u origin main


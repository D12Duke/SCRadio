"""Standalone diagnostic: prints every T-press for 10 seconds.
Run as admin if you want to verify hook reaches a foreground game.
"""
import time
import keyboard

count = [0]

def on_t(_e):
    count[0] += 1
    print("T pressed #%d at %.2f" % (count[0], time.time()))

keyboard.on_press_key("t", on_t, suppress=False)
print("Press T in any window for 10 seconds...")
time.sleep(10)
print("Done. Total T presses captured:", count[0])

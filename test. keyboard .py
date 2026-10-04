import time
import pyautogui

print("=== SKRIP TEST BACKLIT KEYBOARD ===")
print("Pastikan posisi kursor/fokus aman dan jangan menyentuh keyboard selama tes.")
print("Tes akan dimulai dalam 3 detik...\n")


print("1. Mencoba menaikkan/menyalakan backlit (Menekan F4)...")
pyautogui.press('f4')
time.sleep(2)

print("2. Mencoba menurunkan/mematikan backlit (Menekan F3)...")
pyautogui.press('f4')

print("\nSelesai! Apakah lampu backlit keyboard Anda merespons naik/turun?")
#!/usr/bin/env python3
import sys
import requests
import socket
import time

def check_telegram_api():
    """Check if we can connect to Telegram API specifically"""
    try:
        # Test connection to Telegram API
        response = requests.get("https://api.telegram.org", timeout=10)
        if response.status_code < 400:
            print("✓ Successfully connected to Telegram API")
            return True
        else:
            print(f"✗ Telegram API returned status code: {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"✗ Failed to connect to Telegram API: {e}")
        return False

def check_network_connectivity():
    """Check general network connectivity"""
    test_urls = [
        "https://www.google.com",
        "https://1.1.1.1"
    ]
    
    for url in test_urls:
        try:
            response = requests.get(url, timeout=10)
            if response.status_code < 400:
                print(f"✓ Successfully connected to {url}")
                return True
        except requests.exceptions.RequestException as e:
            print(f"✗ Failed to connect to {url}: {e}")
    
    # Try socket connection as fallback
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        print("✓ Successfully connected via socket")
        return True
    except OSError as e:
        print(f"✗ Socket connection failed: {e}")
    
    return False

if __name__ == "__main__":
    # First check Telegram API specifically
    print("Checking Telegram API connectivity...")
    telegram_ok = check_telegram_api()
    
    # If Telegram API fails, check general network
    if not telegram_ok:
        print("Checking general network connectivity...")
        network_ok = check_network_connectivity()
        
        if network_ok:
            print("General network is working but Telegram API is unreachable.")
            print("This might be a temporary issue with Telegram's servers.")
            sys.exit(2)  # Special exit code for Telegram-specific issues
        else:
            print("No network connectivity detected.")
            sys.exit(1)
    else:
        print("All network checks passed.")
        sys.exit(0)

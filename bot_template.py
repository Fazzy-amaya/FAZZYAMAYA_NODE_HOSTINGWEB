#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import requests
import socket
import logging
import random
from threading import Thread
from queue import Queue

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger("BotTemplate")

class NetworkManager:
    """Handle network connectivity issues"""
    
    @staticmethod
    def is_connected():
        """Check if we have internet connection"""
        test_urls = [
            "https://www.google.com",
            "https://www.cloudflare.com",
            "https://1.1.1.1"
        ]
        
        for url in test_urls:
            try:
                response = requests.get(url, timeout=10)
                if response.status_code < 400:
                    return True
            except:
                continue
                
        # Fallback to socket connection
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=5)
            return True
        except OSError:
            pass
            
        return False
    
    @staticmethod
    def wait_for_connection(timeout=300, check_interval=10):
        """Wait until network connection is available"""
        logger.info("Checking network connection...")
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if NetworkManager.is_connected():
                logger.info("Network connection established")
                return True
                
            logger.warning(f"No network connection. Retrying in {check_interval} seconds...")
            time.sleep(check_interval)
        
        logger.error("Failed to establish network connection within timeout")
        return False

    @staticmethod
    def robust_request(method, url, max_retries=5, timeout=30, **kwargs):
        """Make HTTP requests with advanced retry logic"""
        for attempt in range(max_retries):
            try:
                response = requests.request(
                    method, 
                    url, 
                    timeout=timeout, 
                    **kwargs
                )
                
                # Retry on server errors (5xx) except on last attempt
                if 500 <= response.status_code < 600 and attempt < max_retries - 1:
                    logger.warning(f"Server error {response.status_code}, retrying...")
                else:
                    return response
                    
            except (requests.ConnectionError, requests.Timeout) as e:
                logger.warning(f"Network error: {e}, retrying...")
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                break
            
            # Wait before retrying with exponential backoff
            wait_time = 2 ** attempt + random.uniform(0, 1)
            time.sleep(wait_time)
        
        logger.error(f"Failed to complete request after {max_retries} attempts")
        return None

class BotWorker(Thread):
    """Base worker class for bot functionality with error handling"""
    
    def __init__(self, task_queue, worker_id):
        super().__init__(daemon=True)
        self.task_queue = task_queue
        self.worker_id = worker_id
        self.running = True
    
    def run(self):
        """Main execution loop with comprehensive error handling"""
        logger.info(f"Worker {self.worker_id} started")
        
        while self.running:
            try:
                task = self.task_queue.get(timeout=5)
                self.process_task(task)
                self.task_queue.task_done()
            except Exception as e:
                logger.error(f"Error in worker {self.worker_id}: {e}")
                time.sleep(2)  # Prevent tight loop on errors
    
    def process_task(self, task):
        """Process a single task - to be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement process_task")
    
    def safe_stop(self):
        """Safely stop the worker"""
        self.running = False
        logger.info(f"Worker {self.worker_id} stopping")

class ExampleBotWorker(BotWorker):
    """Example implementation of a bot worker"""
    
    def process_task(self, task):
        """Example task processing with error handling"""
        logger.info(f"Worker {self.worker_id} processing task: {task}")
        
        try:
            # Simulate some work
            time.sleep(1)
            
            # Simulate occasional failures for testing
            if random.random() < 0.1:  # 10% chance of failure
                raise Exception("Simulated task failure")
                
            logger.info(f"Worker {self.worker_id} completed task: {task}")
            
        except Exception as e:
            logger.error(f"Task {task} failed in worker {self.worker_id}: {e}")
            # Optionally re-queue the task or handle the error

class BotTemplate:
    """Main bot template class with comprehensive management"""
    
    def __init__(self, config=None):
        self.config = config or {}
        self.task_queue = Queue()
        self.workers = []
        self.running = False
        
        # Setup signal handlers for graceful shutdown
        import signal
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
    
    def initialize(self):
        """Initialize the bot with comprehensive setup"""
        logger.info("Initializing bot...")
        
        # Wait for network connection
        if not NetworkManager.wait_for_connection():
            logger.error("Cannot proceed without network connection")
            return False
        
        # Initialize workers based on configuration
        worker_count = self.config.get('worker_count', 3)
        for i in range(worker_count):
            worker = ExampleBotWorker(self.task_queue, i)
            self.workers.append(worker)
        
        logger.info("Bot initialized successfully")
        return True
    
    def start(self):
        """Start the bot with proper initialization"""
        if not self.initialize():
            return False
        
        self.running = True
        logger.info("Starting bot...")
        
        # Start workers
        for worker in self.workers:
            worker.start()
        
        # Main loop
        try:
            task_id = 0
            while self.running:
                # Add tasks to the queue
                self.task_queue.put(f"Task_{task_id}")
                task_id += 1
                
                # Check if we should stop
                time.sleep(self.config.get('task_interval', 5))
                
        except KeyboardInterrupt:
            logger.info("Shutdown requested by user")
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
        finally:
            self.stop()
        
        return True
    
    def stop(self):
        """Stop the bot gracefully"""
        if not self.running:
            return
            
        self.running = False
        logger.info("Stopping bot...")
        
        # Stop workers
        for worker in self.workers:
            worker.safe_stop()
        
        # Wait for workers to finish
        for worker in self.workers:
            worker.join(timeout=10)
        
        # Clear task queue
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
                self.task_queue.task_done()
            except:
                break
        
        logger.info("Bot stopped gracefully")

def main():
    """Main function with command line argument parsing"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Bot Template')
    parser.add_argument('--install', action='store_true', help='Install dependencies only')
    parser.add_argument('--workers', type=int, default=3, help='Number of worker threads')
    parser.add_argument('--interval', type=int, default=5, help='Task interval in seconds')
    
    args = parser.parse_args()
    
    # Start the bot with configuration
    config = {
        'worker_count': args.workers,
        'task_interval': args.interval
    }
    
    bot = BotTemplate(config)
    bot.start()

if __name__ == "__main__":
    main()

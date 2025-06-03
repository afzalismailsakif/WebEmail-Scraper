import csv
import requests
from bs4 import BeautifulSoup
import re
import time
from urllib.parse import urljoin, urlparse
import io 
import os
import uuid
import threading
import logging # Using Python's logging module

from flask import Flask, render_template, request, Response, send_file, jsonify, current_app

app = Flask(__name__)

# --- Configuration ---
REQUEST_TIMEOUT = 10
MAX_DEPTH = 1 
POLITE_DELAY = 1
TARGET_PAGE_KEYWORDS = [
    'about', 'contact', 'support', 
    'contact-us', 'about-us', 'contactus', 'aboutus'
]
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 FlaskEmailScraperBot/1.0'
}
TEMP_DIR = "temp_files" # For storing generated CSVs
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Task Management ---
tasks = {} # Stores status and data for ongoing/completed tasks
tasks_lock = threading.Lock() # To safely access the tasks dictionary

# --- Helper Functions (Modified to log progress to task object) ---
def is_valid_url(url):
    parsed = urlparse(url)
    return bool(parsed.scheme) and bool(parsed.netloc) and parsed.scheme not in ['mailto', 'tel']

def find_emails_on_page(url, content):
    email_regex = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    emails_found = set(re.findall(email_regex, content))
    filtered_emails = set()
    common_image_extensions = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.tiff']
    false_positive_patterns = ['example.com', 'yourdomain.com', 'email@domain.com', 'sentry.io']
    for email in emails_found:
        email_lower = email.lower()
        if any(ext in email_lower for ext in common_image_extensions): continue
        if any(fp_pattern in email_lower for fp_pattern in false_positive_patterns): continue
        if len(email_lower) > 255 or ' ' in email_lower or email_lower.count('@') != 1: continue
        parts = email_lower.split('@')
        if len(parts) == 2 and '.' in parts[1] and len(parts[1].split('.')[-1]) >= 2:
            if not (email_lower.startswith("'") or email_lower.endswith("'")):
                 filtered_emails.add(email_lower)
    return filtered_emails

def scrape_website_emails_for_task(base_url, task_id, max_depth_setting=1, delay=1):
    """Scrapes a website, logging progress to the tasks object."""
    if not is_valid_url(base_url):
        with tasks_lock: # Ensure thread-safe access to tasks dictionary
            if task_id in tasks and 'progress_messages' in tasks[task_id]:
                 tasks[task_id]['progress_messages'].append(f"Skipping invalid base URL: {base_url}")
            else:
                logging.warning(f"Task {task_id} not found or progress_messages missing when skipping invalid URL {base_url}")
        return set()

    visited_urls = set()
    emails_collected = set()
    urls_to_visit = [(base_url, 0)]
    base_domain = urlparse(base_url).netloc
    
    log_progress(task_id, f"Starting scrape for: {base_url} with depth {max_depth_setting}")

    while urls_to_visit:
        current_url, current_processing_depth = urls_to_visit.pop(0)
        if current_url in visited_urls or not is_valid_url(current_url): continue
        if urlparse(current_url).netloc != base_domain: continue
        
        visited_urls.add(current_url)
        page_type = "Homepage" if current_processing_depth == 0 else "Target Page"
        log_progress(task_id, f"Scraping ({page_type}): {current_url}")

        try:
            time.sleep(delay)
            response = requests.get(current_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            page_text = soup.get_text(separator=' ')
            for a_tag in soup.find_all('a', href=True):
                href = a_tag.get('href', '')
                if href.startswith('mailto:'):
                    try:
                        email_in_mailto = href.split('mailto:')[1].split('?')[0]
                        page_text += " " + email_in_mailto
                    except IndexError:
                        log_progress(task_id, f"Warning: Malformed mailto link on {current_url}: {href}")
            
            emails_on_page = find_emails_on_page(current_url, page_text)
            if emails_on_page:
                log_progress(task_id, f"  Found emails: {', '.join(emails_on_page)} on {current_url}")
                emails_collected.update(emails_on_page)

            if max_depth_setting >= 1 and current_processing_depth == 0:
                log_progress(task_id, f"  Searching for target pages on {current_url}...")
                found_target_page_on_current = False
                for link in soup.find_all('a', href=True):
                    href_attr_original = link.get('href')
                    if not href_attr_original: continue
                    href_attr_lower = href_attr_original.lower()
                    link_text_lower = link.get_text(separator=' ').lower()
                    is_target_link = any(keyword in href_attr_lower or keyword in link_text_lower for keyword in TARGET_PAGE_KEYWORDS)
                    if is_target_link:
                        next_url = urljoin(current_url, href_attr_original)
                        if urlparse(next_url).netloc == base_domain and \
                           next_url not in visited_urls and is_valid_url(next_url) and \
                           next_url not in [item[0] for item in urls_to_visit]:
                            urls_to_visit.append((next_url, current_processing_depth + 1))
                            log_progress(task_id, f"    Queued target page: {next_url}")
                            found_target_page_on_current = True
                if not found_target_page_on_current:
                    log_progress(task_id, f"  No new target pages found or queued from {current_url}.")
        except Exception as e:
            log_progress(task_id, f"  Error scraping {current_url}: {str(e)}")
    return emails_collected

def log_progress(task_id, message):
    """Appends a message to the task's progress log."""
    with tasks_lock:
        if task_id in tasks:
            # Ensure progress_messages list exists
            if 'progress_messages' not in tasks[task_id]:
                tasks[task_id]['progress_messages'] = []
            tasks[task_id]['progress_messages'].append(message)
        else:
            logging.warning(f"Attempted to log progress for non-existent task_id: {task_id}")


def run_scraping_task_thread(task_id, websites_to_process):
    """The function that will run in a separate thread to perform scraping."""
    try:
        with tasks_lock:
            tasks[task_id]['status'] = 'processing'
        
        # This list will store tuples of (website_url, "comma_separated_emails_string")
        all_scraped_data_consolidated = [] 
        total_websites = len(websites_to_process)

        for i, website_url in enumerate(websites_to_process):
            log_progress(task_id, f"--- Processing website {i+1}/{total_websites}: {website_url} ---")
            # scrape_website_emails_for_task returns a set of unique emails for the current website
            emails_found_for_site = scrape_website_emails_for_task(website_url, task_id, max_depth_setting=MAX_DEPTH, delay=POLITE_DELAY)
            
            if emails_found_for_site:
                # Join all found emails for this site into a single string, separated by commas
                emails_string = ", ".join(sorted(list(emails_found_for_site))) # Sort for consistent order
                all_scraped_data_consolidated.append((website_url, emails_string))
                log_progress(task_id, f"  Successfully scraped {len(emails_found_for_site)} email(s) from {website_url}: {emails_string}")
            else:
                all_scraped_data_consolidated.append((website_url, "NO_EMAILS_FOUND_OR_ERROR"))
                log_progress(task_id, f"  No emails found for {website_url} (or an error occurred).")
        
        # Save to CSV
        csv_filename = f"scraped_emails_{task_id}.csv"
        csv_filepath = os.path.join(TEMP_DIR, csv_filename)
        
        with open(csv_filepath, mode='w', newline='', encoding='utf-8') as outfile:
            writer = csv.writer(outfile)
            # Updated CSV Header
            writer.writerow(['Website', 'Emails Found']) 
            writer.writerows(all_scraped_data_consolidated) # Write the consolidated data
        
        log_progress(task_id, f"Scraping complete. Results saved to server: {csv_filename}")
        with tasks_lock:
            tasks[task_id]['status'] = 'complete'
            tasks[task_id]['filename'] = csv_filename
            tasks[task_id]['results_summary'] = f"{len(all_scraped_data_consolidated)} websites processed."

    except Exception as e:
        logging.error(f"Error in scraping task {task_id}: {e}", exc_info=True)
        with tasks_lock:
            if task_id in tasks: # Check if task still exists
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error_message'] = str(e)
                log_progress(task_id, f"A critical error occurred: {str(e)}")
            else:
                logging.error(f"Task {task_id} not found when trying to report error: {e}")


# --- Flask Routes ---
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html', MAX_DEPTH=MAX_DEPTH)

@app.route('/request-scrape', methods=['POST'])
def request_scrape():
    urls_text = request.form.get('urls')
    if not urls_text:
        return jsonify({"error": "Please provide some URLs."}), 400

    raw_urls = urls_text.strip().splitlines()
    websites_to_process = []
    for u in raw_urls:
        url = u.strip()
        if url:
            if not url.startswith(('http://', 'https://')):
                url = 'http://' + url
            websites_to_process.append(url)
    
    if not websites_to_process:
        return jsonify({"error": "No valid URLs to process."}), 400

    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {
            'status': 'pending',
            'urls': websites_to_process,
            'progress_messages': ["Task initiated."], # Initialize progress_messages
            'filename': None,
            'error_message': None,
            'last_message_index_sent': -1 
        }
    
    thread = threading.Thread(target=run_scraping_task_thread, args=(task_id, websites_to_process))
    thread.daemon = True 
    thread.start()
    
    return jsonify({"task_id": task_id})

@app.route('/progress-stream/<task_id>')
def progress_stream(task_id):
    def generate():
        last_sent_idx = -1
        while True:
            with tasks_lock:
                if task_id not in tasks:
                    yield f"data: Error: Task ID {task_id} not found.\n\n"
                    break
                
                current_task = tasks[task_id]
                # Ensure progress_messages exists before trying to access it
                current_progress_messages = current_task.get('progress_messages', [])
                new_messages = current_progress_messages[last_sent_idx+1:]

                for msg in new_messages:
                    yield f"data: {msg}\n\n"
                
                last_sent_idx += len(new_messages)

                if current_task['status'] == 'complete':
                    yield f"data: COMPLETE:{current_task['filename']}\n\n"
                    break
                elif current_task['status'] == 'error':
                    error_msg = current_task.get('error_message', 'An unknown error occurred.')
                    yield f"data: ERROR:{error_msg}\n\n"
                    break
            
            time.sleep(0.5) 

    return Response(generate(), mimetype='text/event-stream')

@app.route('/download/<filename>')
def download_file(filename):
    if ".." in filename or filename.startswith("/"): 
        return "Invalid filename", 400
    
    file_path = os.path.join(TEMP_DIR, filename)
    if not os.path.isfile(file_path):
        return "File not found or access denied.", 404
        
    return send_file(file_path, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    logging.info("Flask Email Scraper starting...")
    app.run(debug=True, threaded=True)

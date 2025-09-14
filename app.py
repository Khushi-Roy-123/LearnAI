from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import Markup
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
import praw
import json
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from google import genai
import logging
import markdown
import re
import os
import pickle
from concurrent.futures import ThreadPoolExecutor, as_completed
from fpdf import FPDF
from datetime import datetime

# Initialize Flask app
app = Flask(__name__)
app.secret_key = "your_secret_key"  # Replace with a secure random string
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(BASE_DIR, "instance", "learnsphere.db")
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_path}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    queries = db.relationship('Query', backref='user', lazy=True)
    chats = db.relationship('Chat', backref='user', lazy=True)
    timelines = db.relationship('Timeline', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Query(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    query_text = db.Column(db.String(200), nullable=False)
    course_name = db.Column(db.String(200), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_name = db.Column(db.String(200), nullable=False)
    chat_query = db.Column(db.Text, nullable=False)
    response = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Timeline(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_name = db.Column(db.String(200), nullable=False)
    data = db.Column(db.Text, nullable=False)  # JSON string for timeline HTML

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Reddit API credentials
reddit = praw.Reddit(
    client_id='5sqb8F0WLRiyXkVOC2hgtQ',
    client_secret='GvfvUyydRbPcteUCvBeNcflh9rhYbQ',
    user_agent='course_recommender v1.0'
)

# Gemini API client
GEMINI_API_KEY = 'AIzaSyDQXAJXhYxu6KV7Zp1o56y6-Xfm6Gxt_fg'
client = genai.Client(api_key=GEMINI_API_KEY)

analyzer = SentimentIntensityAnalyzer()

# Cache directory
CACHE_DIR = 'cache'
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

def get_cache_path(query, prefix):
    """Generate cache file path for query."""
    return os.path.join(CACHE_DIR, f"{prefix}_{query.replace(' ', '_').lower()}.pkl")

def load_cache(cache_path):
    """Load cached data if available."""
    if os.path.exists(cache_path):
        with open(cache_path, 'rb') as f:
            return pickle.load(f)
    return None

def save_cache(data, cache_path):
    """Save data to cache without limits."""
    with open(cache_path, 'wb') as f:
        pickle.dump(data, f)

import markdown
from bs4 import BeautifulSoup

def clean_markdown_for_html(markdown_text):
    """
    Convert Markdown to clean HTML with proper table formatting
    """
    try:
        # Handle case where markdown_text is a list instead of string
        if isinstance(markdown_text, list):
            markdown_text = '\n'.join(str(item) for item in markdown_text)
        elif not isinstance(markdown_text, str):
            markdown_text = str(markdown_text)
        
        # Check if it's already a table format
        if '|' in markdown_text and ('Week' in markdown_text or 'Phase' in markdown_text):
            # It's already in table format, process normally
            html_content = markdown.markdown(markdown_text, extensions=['tables'])
        else:
            # Convert text to table format
            lines = markdown_text.split('\n')
            table_lines = ["| Week/Phase | Topic/Skill | Practical Task | Estimated Duration |", 
                          "|------------|-------------|----------------|-------------------|"]
            
            current_section = None
            for line in lines:
                line = line.strip()
                if line.startswith('#') or line.startswith('**'):
                    # This is a section header
                    if current_section:
                        table_lines.append(f"| {current_section['phase']} | {current_section['topics']} | {current_section['tasks']} | {current_section['duration']} |")
                    
                    current_section = {
                        'phase': line.replace('#', '').replace('*', '').strip(),
                        'topics': '',
                        'tasks': '',
                        'duration': ''
                    }
                elif line and current_section:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        key = key.strip().lower()
                        value = value.strip()
                        if 'topic' in key:
                            current_section['topics'] = value
                        elif 'task' in key or 'project' in key or 'practical' in key:
                            current_section['tasks'] = value
                        elif 'duration' in key or 'time' in key or 'week' in key:
                            current_section['duration'] = value
                    else:
                        # Add to topics as fallback
                        current_section['topics'] += " " + line if current_section['topics'] else line
            
            if current_section:
                table_lines.append(f"| {current_section['phase']} | {current_section['topics']} | {current_section['tasks']} | {current_section['duration']} |")
            
            markdown_text = '\n'.join(table_lines)
            html_content = markdown.markdown(markdown_text, extensions=['tables'])
        
        # Clean up the HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Add classes to tables for styling
        for table in soup.find_all('table'):
            table['class'] = 'timeline-table'
        
        return str(soup)
    
    except Exception as e:
        print(f"Error converting Markdown to HTML: {e}")
        # Fallback: return basic formatted text
        if isinstance(markdown_text, list):
            content = '<br>'.join(str(item) for item in markdown_text)
        else:
            content = str(markdown_text).replace('\n', '<br>')
        return f"<div class='timeline-content'>{content}</div>"

@app.route('/chat/<course_name>', methods=['GET', 'POST'])
@login_required
def chat(course_name):
    timeline_entry = Timeline.query.filter_by(user_id=current_user.id, course_name=course_name).first()
    if not timeline_entry:
        logger.error(f"No timeline found for course: {course_name}")
        flash('Timeline not found')
        return redirect(url_for('dashboard'))
    
    # Deserialize the timeline from JSON - this should be the original Markdown
    try:
        timeline_markdown = json.loads(timeline_entry.data)
        logger.info(f"Deserialized timeline for {course_name}, length: {len(str(timeline_markdown))} characters")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to deserialize timeline for {course_name}: {e}")
        flash('Error loading timeline')
        return redirect(url_for('dashboard'))
    
    # Convert Markdown to HTML for display
    timeline_html = clean_markdown_for_html(timeline_markdown)
    
    chats = Chat.query.filter_by(user_id=current_user.id, course_name=course_name).order_by(Chat.timestamp.asc()).all()
    
    if request.method == 'POST':
        if request.is_json:
            query = request.json.get('message')
        else:
            query = request.form.get('query')
        
        logger.info(f"Processing chat query: {query}")
        try:
            # Use the original Markdown (not HTML) for the prompt
            prompt = (
                f"Here is the current learning timeline in Markdown table format:\n\n{timeline_markdown}\n\n"
                f"Modify this timeline based on the user's request: '{query}'. "
                f"IMPORTANT: Maintain the exact same Markdown table format with columns: Week/Phase | Topic/Skill | Practical Task | Estimated Duration. "
                f"Only modify the content within the table cells. Do not change the table structure. "
                f"Also maintain the Career Options and Next Steps tables if they exist. "
                f"Return ONLY the complete updated Markdown tables, no additional text or explanations."
                f"DO NOT wrap the response in markdown code blocks (do not use ```markdown or ```). "
            )
            
            response_obj = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            logger.info(f"Raw Gemini response: {response_obj.text[:200]}...")
            
            # Store the updated Markdown
            updated_timeline_md = response_obj.text
            
            # Convert to HTML for display
            updated_timeline_html = clean_markdown_for_html(updated_timeline_md)
            
            response = "Timeline updated based on your request."
            logger.info(f"Updated timeline length: {len(updated_timeline_html)} characters")
            
        except Exception as e:
            logger.warning(f"Error updating timeline: {e}")
            response = f"Error updating timeline: {e}"
            updated_timeline_md = timeline_markdown
            updated_timeline_html = timeline_html

        # Save chat history
        chat = Chat(
            user_id=current_user.id,
            course_name=course_name,
            chat_query=query,
            response=response,
            timestamp=datetime.utcnow()
        )
        db.session.add(chat)
        
        # Update the timeline with the new Markdown
        timeline_entry.data = json.dumps(updated_timeline_md)
        db.session.commit()
        
        logger.info(f"Saved updated timeline for {course_name}")
        return jsonify({
            'response': response, 
            'timeline': updated_timeline_html,
            'timeline_markdown': updated_timeline_md  # Also return Markdown for debugging
        })

    return render_template('chat.html', course_name=course_name, chats=chats, timeline=timeline_html)
def fetch_course_details(cc_link, headers, name, provider, description):
    """Fetch details for a single course."""
    try:
        logger.info(f"Fetching details for {name}")
        cc_response = requests.get(cc_link, headers=headers, timeout=10)
        cc_response.raise_for_status()
        cc_soup = BeautifulSoup(cc_response.text, 'html.parser')
        direct_link_elem = cc_soup.select_one('a.btn.btn-primary[href^="https://"]')
        direct_link = direct_link_elem['href'] if direct_link_elem and 'classcentral' not in direct_link_elem['href'] else None

        if not direct_link:
            provider_lower = provider.lower()
            if 'coursera' in provider_lower:
                direct_link = cc_soup.select_one('a[href*="coursera.org"]')
            elif 'udemy' in provider_lower:
                direct_link = cc_soup.select_one('a[href*="udemy.com"]')
            elif 'edx' in provider_lower:
                direct_link = cc_soup.select_one('a[href*="edx.org"]')
            elif 'freecodecamp' in provider_lower:
                direct_link = cc_soup.select_one('a[href*="freecodecamp.org"]')
            elif 'udacity' in provider_lower:
                direct_link = cc_soup.select_one('a[href*="udacity.com"]')
            direct_link = direct_link['href'] if direct_link else f"https://www.{provider_lower}.org"

        workload_elem = cc_soup.select_one('span[aria-label="Workload and duration"]')
        workload = workload_elem.text.strip() if workload_elem else 'Not specified'

        start_date_elem = cc_soup.select_one('span[aria-label="Start date"]')
        start_date = start_date_elem.text.strip() if start_date_elem else 'On-Demand'

        num_courses_elem = cc_soup.select_one('span[aria-label="Number of courses"]')
        num_courses = num_courses_elem.text.strip() if num_courses_elem else '1 course'

        full_description_elem = cc_soup.select_one('div.course-description')
        full_description = full_description_elem.text.strip() if full_description_elem else description

        return {
            'direct_link': direct_link,
            'workload': workload,
            'start_date': start_date,
            'num_courses': num_courses,
            'description': full_description
        }
    except Exception as e:
        logger.warning(f"Failed to fetch details for {name}: {e}")
        return {
            'direct_link': f"https://www.{provider.lower()}.org",
            'workload': 'Not specified',
            'start_date': 'On-Demand',
            'num_courses': '1 course',
            'description': description
        }

def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    logger.info(f"Accessing /login with next={request.args.get('next')}")
    if current_user.is_authenticated:
        logger.info("User is authenticated, redirecting to dashboard")
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get('next')
            if next_page and is_safe_url(next_page):
                logger.info(f"Redirecting to next_page: {next_page}")
                return redirect(next_page)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('Email already exists')
            return render_template('register.html')
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    queries = Query.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', queries=queries)
import markdown
from bs4 import BeautifulSoup

def clean_markdown_for_html(markdown_text):
    """
    Convert Markdown to clean HTML and remove any unwanted characters
    """
    try:
        # Handle case where markdown_text is a list instead of string
        if isinstance(markdown_text, list):
            # Convert list to string by joining with newlines
            markdown_text = '\n'.join(str(item) for item in markdown_text)
        elif not isinstance(markdown_text, str):
            # Convert any other type to string
            markdown_text = str(markdown_text)
            
        # Convert Markdown to HTML with table support
        html_content = markdown.markdown(markdown_text, extensions=['tables'])
        
        # Clean up the HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Add classes to tables for styling
        for table in soup.find_all('table'):
            table['class'] = 'timeline-table'
        
        # Return clean HTML
        return str(soup)
    
    except Exception as e:
        print(f"Error converting Markdown to HTML: {e}")
        # Fallback: handle list case in error scenario too
        if isinstance(markdown_text, list):
            content = '<br>'.join(str(item) for item in markdown_text)
        else:
            content = str(markdown_text).replace('\n', '<br>')
        return f"<div class='timeline-content'>{content}</div>"

@app.route('/recommend', methods=['POST'])
@login_required
def recommend():
    query = request.form['query']
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    course_cache_path = get_cache_path(query, 'courses')
    cached_courses = load_cache(course_cache_path)
    if cached_courses:
        logger.info(f"Using cached courses for query: {query}")
        courses = cached_courses
    else:
        search_url = f"https://www.classcentral.com/search?q=%22{query.replace(' ', '+')}%22"
        courses = []
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"Attempt {attempt + 1} to fetch courses from Class Central for query: {query}")
                response = requests.get(search_url, headers=headers, timeout=10)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                break
            except requests.RequestException as e:
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"Failed to fetch courses from Class Central after {max_retries} attempts: {e}")
                    courses = [
                        {
                            'name': f"Introduction to {query}",
                            'provider': 'Coursera',
                            'institution': 'University of Michigan',
                            'direct_link': 'https://www.coursera.org/learn/python',
                            'description': f"Learn the fundamentals of {query} programming.",
                            'workload': '10-15 hours',
                            'start_date': 'On-Demand',
                            'pricing': 'Free without certificate',
                            'num_courses': '1 course',
                            'subject': 'Programming',
                            'level': 'Beginner',
                            'cc_rating': 4.5,
                            'cc_num_reviews': 1000,
                            'score': 0
                        },
                        {
                            'name': f"{query} for Everybody",
                            'provider': 'Udemy',
                            'institution': '',
                            'direct_link': 'https://www.udemy.com/course/python-for-everybody',
                            'description': f"Comprehensive {query} course for all skill levels.",
                            'workload': '20 hours',
                            'start_date': 'On-Demand',
                            'pricing': 'Pay for certificate',
                            'num_courses': '1 course',
                            'subject': 'Programming',
                            'level': 'Intermediate',
                            'cc_rating': 4.7,
                            'cc_num_reviews': 500,
                            'score': 0
                        },
                        {
                            'name': f"{query} Programming",
                            'provider': 'edX',
                            'institution': 'MIT',
                            'direct_link': 'https://www.edx.org/course/introduction-to-python',
                            'description': f"Advanced {query} programming concepts.",
                            'workload': '15-20 hours',
                            'start_date': 'On-Demand',
                            'pricing': 'Pay for certificate',
                            'num_courses': '1 course',
                            'subject': 'Programming',
                            'level': 'Advanced',
                            'cc_rating': 4.8,
                            'cc_num_reviews': 300,
                            'score': 0
                        },
                        {
                            'name': f"Learn {query} the Hard Way",
                            'provider': 'freeCodeCamp',
                            'institution': '',
                            'direct_link': 'https://www.freecodecamp.org/learn',
                            'description': f"Practical {query} course with hands-on exercises.",
                            'workload': '25 hours',
                            'start_date': 'On-Demand',
                            'pricing': 'Free without certificate',
                            'num_courses': '1 course',
                            'subject': 'Programming',
                            'level': 'Beginner',
                            'cc_rating': 4.6,
                            'cc_num_reviews': 400,
                            'score': 0
                        },
                        {
                            'name': f"{query} Nanodegree",
                            'provider': 'Udacity',
                            'institution': '',
                            'direct_link': 'https://www.udacity.com/course/python-nanodegree',
                            'description': f"Project-based {query} learning for professionals.",
                            'workload': '30 hours',
                            'start_date': 'On-Demand',
                            'pricing': 'Pay for certificate',
                            'num_courses': '1 course',
                            'subject': 'Programming',
                            'level': 'Intermediate',
                            'cc_rating': 4.9,
                            'cc_num_reviews': 200,
                            'score': 0
                        }
                    ]
                    break

        if not courses:
            course_items = soup.select('li.course-list-course')[:5]
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_course = {}
                for item in course_items:
                    name_elem = item.select_one('h2[itemprop="name"]')
                    link_elem = item.select_one('a.course-name[itemprop="url"]')
                    props_str = item.select_one('a[data-track-props]')['data-track-props'] if item.select_one('a[data-track-props]') else '{}'
                    try:
                        props = json.loads(props_str.replace('&quot;', '"'))
                        provider = props.get('course_provider', 'Unknown')
                        institution = props.get('course_institution', '')
                        cc_rating = props.get('course_avg_rating', 0)
                        cc_num_reviews = props.get('course_num_rating', 0)
                        subject = props.get('course_subject', '')
                        level = props.get('course_level', '')
                        is_free = props.get('course_is_free', False)
                        certificate = props.get('course_certificate', False)
                        pricing_status = 'Free without certificate' if is_free and not certificate else 'Pay for certificate' if certificate else 'Paid Course'
                    except json.JSONDecodeError:
                        provider = 'Unknown'
                        institution = ''
                        cc_rating = 0
                        cc_num_reviews = 0
                        subject = ''
                        level = ''
                        is_free = False
                        certificate = False
                        pricing_status = 'Paid Course'

                    description_elem = item.select_one('p.text-2.margin-bottom-xsmall')
                    description = description_elem.text.strip() if description_elem else 'No description available'

                    if name_elem and link_elem:
                        name = name_elem.text.strip()
                        cc_link = 'https://www.classcentral.com' + link_elem['href']
                        future_to_course[executor.submit(fetch_course_details, cc_link, headers, name, provider, description)] = {
                            'name': name,
                            'provider': provider,
                            'institution': institution,
                            'description': description,
                            'subject': subject,
                            'level': level,
                            'pricing': pricing_status,
                            'cc_rating': cc_rating,
                            'cc_num_reviews': cc_num_reviews,
                            'score': 0
                        }

                for future in as_completed(future_to_course):
                    course_data = future_to_course[future]
                    try:
                        details = future.result()
                        course_data.update(details)
                        courses.append(course_data)
                    except Exception as e:
                        logger.warning(f"Error processing course {course_data['name']}: {e}")
                        courses.append(course_data)

        save_cache(courses, course_cache_path)

    if not courses:
        flash("No courses found for '{}'".format(query))
        return render_template('result.html')

    for course in courses:
        reddit_query = f'"{course["name"]}" {course["provider"]} {query} review'
        if course['institution']:
            reddit_query += f' "{course["institution"]}"'
        comments = []
        try:
            for submission in reddit.subreddit('all').search(reddit_query, limit=5):
                submission.comments.replace_more(limit=0)
                comments.extend([comment.body for comment in submission.comments.list()[:5]])
        except Exception as e:
            logger.warning(f"Error fetching Reddit data for {course['name']}: {e}")

        scores = [analyzer.polarity_scores(c)['compound'] for c in comments if c]
        reddit_sentiment = sum(scores) / len(scores) if scores else 0
        normalized_reddit = (reddit_sentiment + 1) * 2.5 if reddit_sentiment else 0
        final_score = 0.9 * course['cc_rating'] + 0.1 * normalized_reddit
        course['score'] = final_score

    courses.sort(key=lambda x: x['score'], reverse=True)
    best_course = courses[0]

    required_keys = ['workload', 'start_date', 'num_courses', 'description', 'direct_link']
    for key in required_keys:
        if key not in best_course:
            best_course[key] = 'Not specified' if key != 'direct_link' else f"https://www.{best_course['provider'].lower()}.org"

    timeline_cache_path = get_cache_path(query, 'timeline')
    cached_timeline = load_cache(timeline_cache_path)
    if cached_timeline:
        logger.info(f"Using cached timeline for query: {query}")
        timeline_markdown = cached_timeline  # Store as markdown
    else:
        try:
            prompt = (
                f"Create a professional learning timeline for mastering '{query}' using '{best_course['name']}' "
                f"by {best_course['provider']} ({best_course['institution']}). "
                f"Level: {best_course['level']}, Workload: {best_course['workload']}, Pricing: {best_course['pricing']}. "
                f"Return the timeline as a Markdown table with EXACTLY these columns: "
                f"Week/Phase | Topic/Skill | Practical Task | Estimated Duration. "
                f"Each row should represent a week or major topic. "
                f"After the main timeline table, add two more Markdown tables: "
                f"1. '### Career Options' table with columns: Role | Description "
                f"2. '### Next Steps' table with columns: Learning Path | Description "
                f"DO NOT include any introductory text, explanations, or text outside the tables. "
                f"ONLY output the Markdown tables, nothing else."
            )
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            timeline_markdown = response.text  # Store the raw Markdown
            save_cache(timeline_markdown, timeline_cache_path)
        except Exception as e:
            logger.warning(f"Failed to generate Gemini timeline: {e}")
            # Create a proper fallback with Markdown table format
            timeline_markdown = (
                "| Week/Phase | Topic/Skill | Practical Task | Estimated Duration |\n"
                "|------------|-------------|----------------|-------------------|\n"
                f"| Week 1 | Introduction to {query} | Setup development environment | 1 Week |\n"
                f"| Week 2-3 | Core {query} Concepts | Build small projects | 2 Weeks |\n"
                f"| Week 4-6 | Advanced {query} Topics | Develop capstone project | 3 Weeks |\n"
                f"| Week 7-8 | Real-world Applications | Portfolio projects | 2 Weeks |\n\n"
                "### Career Options\n\n"
                "| Role | Description |\n"
                "|------|-------------|\n"
                f"| {query} Developer | Develop applications using {query} |\n"
                f"| {query} Specialist | Specialize in {query} technologies |\n\n"
                "### Next Steps\n\n"
                "| Learning Path | Description |\n"
                "|---------------|-------------|\n"
                "| Advanced Certifications | Pursue professional certifications |\n"
                "| Open Source Contribution | Contribute to relevant projects |\n"
                "DO NOT wrap the response in markdown code blocks (do not use ```markdown or ```). "
            )
            save_cache(timeline_markdown, timeline_cache_path)

    # Convert the Markdown timeline to HTML for display ONLY
    timeline_html = clean_markdown_for_html(timeline_markdown)

    query_entry = Query(
        user_id=current_user.id,
        query_text=query,
        course_name=best_course['name'],
        timestamp=datetime.utcnow()
    )
    db.session.add(query_entry)
    
    # Store the original Markdown in the database, NOT HTML
    timeline_entry = Timeline.query.filter_by(user_id=current_user.id, course_name=best_course['name']).first()
    if not timeline_entry:
        timeline_entry = Timeline(
            user_id=current_user.id,
            course_name=best_course['name'],
            data=json.dumps(timeline_markdown)  # Store Markdown, not HTML
        )
        db.session.add(timeline_entry)
    else:
        timeline_entry.data = json.dumps(timeline_markdown)  # Store Markdown, not HTML
        timeline_entry.timestamp = datetime.utcnow()
    
    db.session.commit()

    return render_template(
        'result.html',
        query=query,
        best_course=best_course,
        other_courses=[c for c in courses if c['name'] != best_course['name']],
        timeline=timeline_html  # Pass HTML for display only
    )

@app.route('/course/<course_name>')
@login_required
def course(course_name):
    query = Query.query.filter_by(user_id=current_user.id, course_name=course_name).first()
    if not query:
        flash('Course not found')
        return redirect(url_for('dashboard'))
    
    return recommend_course(query.query_text, course_name)

def process_chat_query(query, timeline, course_name):
    """Process chat query to modify timeline."""
    q = (query or "").strip()
    low = q.lower()
    resp = f"Received your request for {course_name}: {q}"
    upd = None

    def parse_weeks(s):
        m = re.search(r'(\d+)\s*week', s, re.I)
        return f"{m.group(1)} weeks" if m else None

    def clean_html(s):
        return re.sub(r'<[^>]+>', '', s or '')

    def get_title_and_desc(step_text):
        plain = clean_html(step_text)
        if ":" in plain:
            title, desc = plain.split(":", 1)
            return title.strip(), desc.strip()
        return plain.strip(), ""

    norm = []
    for s in timeline:
        t = str(s)
        title, desc = get_title_and_desc(t)
        if not re.search(r'\(\d+\s*weeks?\)', desc, re.I):
            desc = (desc + " (2 weeks)").strip()
        norm.append(Markup(f"<strong>{title}</strong>: {desc}"))

    def set_step(i, title=None, duration_weeks=None, desc_extra=None):
        nonlocal norm
        title0, desc0 = get_title_and_desc(norm[i])
        title1 = title or title0
        if duration_weeks:
            desc0 = re.sub(r'\(\d+\s*weeks?\)', f'({duration_weeks})', desc0)
            if not re.search(r'\(\d+\s*weeks?\)', desc0):
                desc0 = (desc0 + f" ({duration_weeks})").strip()
        if desc_extra is not None:
            desc0 = desc_extra
        norm[i] = Markup(f"<strong>{title1}</strong>: {desc0}")

    m = re.search(r'(shorten|extend)\s+step\s+(\d+)\s+(?:to|by)?\s*(\d+)\s*weeks?', low, re.I)
    if m:
        idx = int(m.group(2)) - 1
        weeks = m.group(3) + " weeks"
        if 0 <= idx < len(norm):
            set_step(idx, duration_weeks=weeks)
            upd = norm
            resp = f"Updated duration of step {idx+1} to {weeks}."
            return resp, upd

    m = re.search(r'(delete|remove)\s+step\s+(\d+)', low, re.I)
    if m:
        idx = int(m.group(2)) - 1
        if 0 <= idx < len(norm):
            removed = clean_html(norm[idx])
            del norm[idx]
            upd = norm
            resp = f"Removed step {idx+1}: {removed.split(':',1)[0]}."
            return resp, upd

    m = re.search(r'rename\s+step\s+(\d+)\s+to\s+[\'\"]?(.+?)[\'\"]?$', q, re.I)
    if m:
        idx = int(m.group(1)) - 1
        new_title = m.group(2).strip()
        if 0 <= idx < len(norm) and new_title:
            set_step(idx, title=new_title)
            upd = norm
            resp = f"Renamed step {idx+1} to '{new_title}'."
            return resp, upd

    m = re.search(r'insert\s+step\s+?[\'\"]?(.+?)[\'\"]?\s*\((\d+)\)\s*(before|after)\s*step\s+(\d+)', q, re.I)
    if m:
        title = m.group(1).strip()
        duration = m.group(2) + " weeks"
        pos = m.group(3).lower()
        ref = int(m.group(4)) - 1
        new_item = Markup(f"<strong>{title}</strong>: Work on {title.lower()} ({duration})")
        if 0 <= ref < len(norm):
            insert_at = ref if pos == "before" else ref + 1
            norm.insert(insert_at, new_item)
            upd = norm
            resp = f"Inserted '{title}' {pos} step {ref+1}."
            return resp, upd

    m = re.search(r'add\s+step\s+?[\'\"]?(.+?)[\'\"]?\s*(?:for\s+)?(\d+)\s*weeks?', q, re.I)
    if m:
        title = m.group(1).strip()
        duration = m.group(2) + " weeks"
        norm.append(Markup(f"<strong>{title}</strong>: Work on {title.lower()} ({duration})"))
        upd = norm
        resp = f"Added new step: {title} ({duration})."
        return resp, upd

    m = re.search(r'move\s+step\s+(\d+)\s+(before|after)\s+step\s+(\d+)', low, re.I)
    if m:
        src = int(m.group(1)) - 1
        pos = m.group(2)
        dst = int(m.group(3)) - 1
        if 0 <= src < len(norm) and 0 <= dst < len(norm):
            item = norm.pop(src)
            insert_at = dst if pos == "before" else dst + 1
            if insert_at > len(norm):
                insert_at = len(norm)
            norm.insert(insert_at, item)
            upd = norm
            resp = f"Moved step {src+1} {pos} step {dst+1}."
            return resp, upd

    m = re.search(r'mark\s+step\s+(\d+)\s+complete', low, re.I)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(norm):
            title, desc = get_title_and_desc(norm[idx])
            if "✅" not in title:
                title = f"✅ {title}"
            norm[idx] = Markup(f"<strong>{title}</strong>: {desc}")
            upd = norm
            resp = f"Marked step {idx+1} complete."
            return resp, upd

    return resp, upd

@app.route('/download_timeline/<course_name>')
@login_required
def download_timeline(course_name):
    try:
        # Debug prints
        print(f"DEBUG: Download requested for course: {course_name}")
        print(f"DEBUG: Current user ID: {current_user.id}")
        
        timeline_entry = Timeline.query.filter_by(user_id=current_user.id, course_name=course_name).first()
        
        if not timeline_entry:
            print("DEBUG: No timeline found in database")
            flash('No timeline found for this course. Please generate a timeline first in the chat page.', 'error')
            return redirect(url_for('dashboard'))
        
        print("DEBUG: Timeline found - processing data")
        
        import re
        import io
        from fpdf import FPDF
        from datetime import datetime

        # Extract Markdown table from timeline
        timeline_data = json.loads(timeline_entry.data)
        
        if isinstance(timeline_data, list):
            timeline_md = '\n'.join(str(item) for item in timeline_data)
        elif not isinstance(timeline_data, str):
            timeline_md = str(timeline_data)
        else:
            timeline_md = timeline_data

        print(f"DEBUG: Timeline data type: {type(timeline_data)}")
        print(f"DEBUG: First 200 chars: {timeline_md[:200]}")

        # Try to find a Markdown table
        table_match = re.search(r'(\|.+\|\n\|[-| ]+\|\n(?:\|.+\|\n?)+)', timeline_md)
        if table_match:
            table_md = table_match.group(1)
            print("DEBUG: Markdown table found")
            # Parse Markdown table into rows
            lines = [line.strip() for line in table_md.strip().split('\n') if line.strip()]
            headers = [h.strip() for h in lines[0].split('|')[1:-1]]
            rows = []
            for line in lines[2:]:
                cols = [c.strip() for c in line.split('|')[1:-1]]
                if len(cols) == len(headers):
                    rows.append(cols)
        else:
            print("DEBUG: No markdown table found, trying to parse as text")
            # Fallback: try to extract steps as lines
            headers = ["Week/Phase", "Topics", "Projects", "Duration"]
            rows = []
            current_week = None
            
            for line in timeline_md.split('\n'):
                line = line.strip()
                if line.startswith('|') and 'Week' in line:
                    # Try to parse as table row without markdown formatting
                    cols = [col.strip() for col in line.split('|')[1:-1]]
                    if len(cols) >= 4:
                        rows.append(cols)
                elif line.startswith('**Week') or line.startswith('Week'):
                    if current_week:
                        rows.append(current_week)
                    current_week = [line.replace('**', '').replace('*', ''), "", "", ""]
                elif line.startswith('**Phase') or line.startswith('Phase'):
                    if current_week:
                        rows.append(current_week)
                    current_week = [line.replace('**', '').replace('*', ''), "", "", ""]
                elif line and current_week:
                    if 'Topics:' in line or 'Topic:' in line:
                        current_week[1] = line.split(':', 1)[1].strip() if ':' in line else line
                    elif 'Projects:' in line or 'Project:' in line or 'Practical' in line:
                        current_week[2] = line.split(':', 1)[1].strip() if ':' in line else line
                    elif 'Duration:' in line or 'Time:' in line:
                        current_week[3] = line.split(':', 1)[1].strip() if ':' in line else line
                    else:
                        # Add to topics if not empty
                        if current_week[1]:
                            current_week[1] += " " + line
                        else:
                            current_week[1] = line
            
            if current_week:
                rows.append(current_week)

        if not rows:
            print("DEBUG: No rows extracted from timeline data")
            flash('Could not parse timeline data. Please regenerate the timeline in chat.', 'error')
            return redirect(url_for('dashboard'))

        print(f"DEBUG: Found {len(rows)} rows in timeline")

        # Create PDF with beautiful styling
        pdf = FPDF()
        pdf.add_page()
        
        # Set margins
        pdf.set_margins(20, 20, 20)
        pdf.set_auto_page_break(auto=True, margin=20)
        
        # Add header with professional design
        pdf.set_fill_color(41, 128, 185)  # Professional blue
        pdf.rect(0, 0, pdf.w, 35, style='F')
        
        # Title
        pdf.set_font("Arial", "B", 22)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 35, f"{course_name} - Weekly Study Plan", ln=True, align='C')
        
        # Subtitle with date
        pdf.set_font("Arial", "I", 10)
        pdf.cell(0, -25, f"Generated on {datetime.now().strftime('%B %d, %Y')}", ln=True, align='C')
        pdf.ln(30)
        
        # Reset text color
        pdf.set_text_color(0, 0, 0)
        
        # Add introduction
        pdf.set_font("Arial", "B", 14)
        pdf.set_text_color(44, 62, 80)
        pdf.cell(0, 10, "Your Learning Journey", ln=True)
        pdf.ln(5)
        
        pdf.set_font("Arial", "", 11)
        pdf.set_text_color(52, 73, 94)
        intro_text = "This roadmap guides you through a structured learning path. Each week builds upon the previous, " \
                    "ensuring you develop skills progressively. Follow this plan week by week to master your learning goals."
        pdf.multi_cell(0, 8, intro_text)
        pdf.ln(15)

        # Weekly study plan section
        pdf.set_font("Arial", "B", 16)
        pdf.set_text_color(41, 128, 185)
        pdf.cell(0, 12, "Weekly Study Plan", ln=True)
        pdf.ln(10)

        # Process each week/phase
        for i, row in enumerate(rows):
            if len(row) >= 4:  # Ensure we have all columns
                # Clean content from markdown
                week_phase = re.sub(r'\*\*|\*|`', '', row[0]).strip()
                topics = re.sub(r'\*\*|\*|`', '', row[1]).strip() if len(row) > 1 else ""
                practical_task = re.sub(r'\*\*|\*|`', '', row[2]).strip() if len(row) > 2 else ""
                duration = re.sub(r'\*\*|\*|`', '', row[3]).strip() if len(row) > 3 else ""
                
                # Week/Phase header
                pdf.set_fill_color(240, 248, 255)  # Light blue background
                pdf.set_text_color(41, 128, 185)
                pdf.set_font("Arial", "B", 12)
                pdf.cell(0, 10, week_phase, ln=True, fill=True)
                pdf.ln(5)
                
                # Topics/Skills
                if topics:
                    pdf.set_text_color(44, 62, 80)
                    pdf.set_font("Arial", "B", 11)
                    pdf.cell(40, 8, "Topics:", ln=0)
                    pdf.set_font("Arial", "", 10)
                    pdf.set_text_color(52, 73, 94)
                    pdf.multi_cell(0, 8, topics)
                    pdf.ln(3)
                
                # Practical Task
                if practical_task:
                    pdf.set_text_color(44, 62, 80)
                    pdf.set_font("Arial", "B", 11)
                    pdf.cell(40, 8, "Projects:", ln=0)
                    pdf.set_font("Arial", "", 10)
                    pdf.set_text_color(52, 73, 94)
                    pdf.multi_cell(0, 8, practical_task)
                    pdf.ln(3)
                
                # Duration
                if duration:
                    pdf.set_text_color(44, 62, 80)
                    pdf.set_font("Arial", "B", 11)
                    pdf.cell(40, 8, "Duration:", ln=0)
                    pdf.set_font("Arial", "", 10)
                    pdf.set_text_color(52, 73, 94)
                    pdf.cell(0, 8, duration)
                    pdf.ln(8)
                
                # Add separator between weeks (not after last one)
                if i < len(rows) - 1:
                    pdf.set_draw_color(200, 200, 200)
                    pdf.line(20, pdf.get_y(), pdf.w - 20, pdf.get_y())
                    pdf.ln(10)

        # Add footer function
        def add_footer():
            pdf.set_y(-15)
            pdf.set_font('Arial', 'I', 8)
            pdf.set_text_color(150, 150, 150)
            pdf.cell(0, 10, f'Page {pdf.page_no()} - {course_name} Learning Plan', 0, 0, 'C')
        
        pdf.footer = add_footer

        # Output PDF with proper encoding
        try:
            pdf_output = pdf.output(dest='S').encode('latin1', 'replace')
        except Exception as e:
            print(f"DEBUG: Latin1 encoding failed: {e}")
            pdf_output = pdf.output(dest='S').encode('utf-8')
        
        pdf_io = io.BytesIO(pdf_output)
        pdf_io.seek(0)
        
        print("DEBUG: PDF generated successfully")
        return send_file(
            pdf_io, 
            as_attachment=True, 
            download_name=f"{course_name.replace(' ', '_')}_study_plan.pdf", 
            mimetype='application/pdf'
        )

    except Exception as e:
        print(f"ERROR in download_timeline: {str(e)}")
        print(f"ERROR Type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        
        flash(f'Error generating PDF: {str(e)}', 'error')
        return redirect(url_for('dashboard'))
def recommend_course(query, course_name):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    course_cache_path = get_cache_path(query, 'courses')
    cached_courses = load_cache(course_cache_path)
    if cached_courses:
        logger.info(f"Using cached courses for query: {query}")
        courses = cached_courses
    else:
        search_url = f"https://www.classcentral.com/search?q=%22{query.replace(' ', '+')}%22"
        courses = []
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.get(search_url, headers=headers, timeout=10)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                break
            except requests.RequestException as e:
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    courses = [
                        {
                            'name': f"Introduction to {query}",
                            'provider': 'Coursera',
                            'institution': 'University of Michigan',
                            'direct_link': 'https://www.coursera.org/learn/python',
                            'description': f"Learn the fundamentals of {query} programming.",
                            'workload': '10-15 hours',
                            'start_date': 'On-Demand',
                            'pricing': 'Free without certificate',
                            'num_courses': '1 course',
                            'subject': 'Programming',
                            'level': 'Beginner',
                            'cc_rating': 4.5,
                            'cc_num_reviews': 1000,
                            'score': 0
                        }
                    ]
                    break

        if not courses:
            course_items = soup.select('li.course-list-course')[:5]
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_course = {}
                for item in course_items:
                    name_elem = item.select_one('h2[itemprop="name"]')
                    link_elem = item.select_one('a.course-name[itemprop="url"]')
                    props_str = item.select_one('a[data-track-props]')['data-track-props'] if item.select_one('a[data-track-props]') else '{}'
                    try:
                        props = json.loads(props_str.replace('&quot;', '"'))
                        provider = props.get('course_provider', 'Unknown')
                        institution = props.get('course_institution', '')
                        cc_rating = props.get('course_avg_rating', 0)
                        cc_num_reviews = props.get('course_num_rating', 0)
                        subject = props.get('course_subject', '')
                        level = props.get('course_level', '')
                        is_free = props.get('course_is_free', False)
                        certificate = props.get('course_certificate', False)
                        pricing_status = 'Free without certificate' if is_free and not certificate else 'Pay for certificate' if certificate else 'Paid Course'
                    except json.JSONDecodeError:
                        provider = 'Unknown'
                        institution = ''
                        cc_rating = 0
                        cc_num_reviews = 0
                        subject = ''
                        level = ''
                        is_free = False
                        certificate = False
                        pricing_status = 'Paid Course'

                    description_elem = item.select_one('p.text-2.margin-bottom-xsmall')
                    description = description_elem.text.strip() if description_elem else 'No description available'

                    if name_elem and link_elem:
                        name = name_elem.text.strip()
                        cc_link = 'https://www.classcentral.com' + link_elem['href']
                        future_to_course[executor.submit(fetch_course_details, cc_link, headers, name, provider, description)] = {
                            'name': name,
                            'provider': provider,
                            'institution': institution,
                            'description': description,
                            'subject': subject,
                            'level': level,
                            'pricing': pricing_status,
                            'cc_rating': cc_rating,
                            'cc_num_reviews': cc_num_reviews,
                            'score': 0
                        }

                for future in as_completed(future_to_course):
                    course_data = future_to_course[future]
                    try:
                        details = future.result()
                        course_data.update(details)
                        courses.append(course_data)
                    except Exception as e:
                        logger.warning(f"Error processing course {course_data['name']}: {e}")
                        courses.append(course_data)

        save_cache(courses, course_cache_path)

    if not courses:
        flash("No courses found for '{}'".format(query))
        return render_template('result.html')

    for course in courses:
        reddit_query = f'"{course["name"]}" {course["provider"]} {query} review'
        if course['institution']:
            reddit_query += f' "{course["institution"]}"'
        comments = []
        try:
            for submission in reddit.subreddit('all').search(reddit_query, limit=5):
                submission.comments.replace_more(limit=0)
                comments.extend([comment.body for comment in submission.comments.list()[:5]])
        except Exception as e:
            logger.warning(f"Error fetching Reddit data for {course['name']}: {e}")

        scores = [analyzer.polarity_scores(c)['compound'] for c in comments if c]
        reddit_sentiment = sum(scores) / len(scores) if scores else 0
        normalized_reddit = (reddit_sentiment + 1) * 2.5 if reddit_sentiment else 0
        final_score = 0.9 * course['cc_rating'] + 0.1 * normalized_reddit
        course['score'] = final_score

    courses.sort(key=lambda x: x['score'], reverse=True)
    best_course = next((course for course in courses if course['name'] == course_name), courses[0])

    required_keys = ['workload', 'start_date', 'num_courses', 'description', 'direct_link']
    for key in required_keys:
        if key not in best_course:
            best_course[key] = 'Not specified' if key != 'direct_link' else f"https://www.{best_course['provider'].lower()}.org"

    timeline_entry = Timeline.query.filter_by(user_id=current_user.id, course_name=course_name).first()
    if timeline_entry:
        try:
            # Get the stored Markdown and convert to HTML for display
            timeline_markdown = json.loads(timeline_entry.data)
            timeline_html = clean_markdown_for_html(timeline_markdown)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to deserialize timeline for {course_name}: {e}")
            # Fallback: generate basic timeline
            timeline_markdown = (
                f"| Week/Phase | Topic/Skill | Practical Task | Estimated Duration |\n"
                f"|------------|-------------|----------------|-------------------|\n"
                f"| Week 1 | Introduction to {query} | Setup and basics | 1 Week |\n"
                f"| Week 2-3 | Core Concepts | Build projects | 2 Weeks |\n"
                f"| Week 4-6 | Advanced Topics | Capstone project | 3 Weeks |\n\n"
                f"### Career Options\n\n"
                f"| Role | Description |\n"
                f"|------|-------------|\n"
                f"| {query} Developer | Build applications |\n\n"
                f"### Next Steps\n\n"
                f"| Learning Path | Description |\n"
                f"|---------------|-------------|\n"
                f"| Certification | Get certified |"
            )
            timeline_html = clean_markdown_for_html(timeline_markdown)
    else:
        # Generate new timeline if not exists
        timeline_cache_path = get_cache_path(query, 'timeline')
        cached_timeline = load_cache(timeline_cache_path)
        if cached_timeline:
            timeline_markdown = cached_timeline
        else:
            try:
                prompt = (
                    f"Create a professional learning timeline for mastering '{query}' "
                    f"Return the timeline as a Markdown table with columns: "
                    f"Week/Phase | Topic/Skill | Practical Task | Estimated Duration. "
                    f"After the main timeline, add Career Options and Next Steps tables."
                    f"DO NOT wrap the response in markdown code blocks (do not use ```markdown or ```). "
                )
                response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
                timeline_markdown = response.text
                save_cache(timeline_markdown, timeline_cache_path)
            except Exception as e:
                logger.warning(f"Failed to generate Gemini timeline: {e}")
                timeline_markdown = (
                    f"| Week/Phase | Topic/Skill | Practical Task | Estimated Duration |\n"
                    f"|------------|-------------|----------------|-------------------|\n"
                    f"| Week 1 | Introduction | Setup and basics | 1 Week |\n"
                    f"| Week 2-3 | Core Concepts | Build projects | 2 Weeks |\n"
                    
                )
        
        timeline_html = clean_markdown_for_html(timeline_markdown)
        
        # Store the Markdown in database
        timeline_entry = Timeline(
            user_id=current_user.id,
            course_name=course_name,
            data=json.dumps(timeline_markdown)
        )
        db.session.add(timeline_entry)
        db.session.commit()

    return render_template('result.html', 
                         query=query, 
                         best_course=best_course, 
                         other_courses=[c for c in courses if c['name'] != course_name], 
                         timeline=timeline_html)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
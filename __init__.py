#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations
import json, time, traceback, threading
from typing import List, Dict, Optional, Set
import urllib.request, urllib.error
# Use more flexible PyQt imports
try:
    from PyQt6.QtWidgets import (QWidget, QFormLayout, QLabel, QLineEdit, QTextEdit,
                                 QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
                                 QHBoxLayout, QApplication, QDialog, QCheckBox)
    from PyQt6.QtCore import Qt
except ImportError:
    from PyQt5.QtWidgets import (QWidget, QFormLayout, QLabel, QLineEdit, QTextEdit,
                                 QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
                                 QHBoxLayout, QApplication, QDialog, QCheckBox)
    from PyQt5.QtCore import Qt

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import info_dialog, error_dialog
from calibre.gui2.preferences import ConfigWidgetBase
from calibre.gui2.threaded_jobs import ThreadedJob
from calibre.utils.config import JSONConfig
from calibre.ebooks.metadata import authors_to_string
from calibre.customize import InterfaceActionBase

# Plugin metadata
__license__ = 'GPL v3'
__copyright__ = '2025'
__docformat__ = 'restructuredtext en'

PLUGIN_NAME = 'Hardcover Sync'
GRAPHQL_ENDPOINT = 'https://api.hardcover.app/v1/graphql'

# Plugin preferences
prefs = JSONConfig('plugins/hardcover_sync')
prefs.defaults['api_token'] = ''
prefs.defaults['sync_owned_only'] = False
prefs.defaults['rate_limit_delay'] = 1.1

# --- API wrapper ---
class HardcoverAPI:
    def __init__(self, token: str, rate_limit_delay: float = 1.1):
        self.token = token
        self.headers = {
            'Authorization': f'Bearer {self.token}',
            #'Authorization': self.token,  # Direct token, not Bearer
            'Content-Type': 'application/json',
            'User-Agent': 'Calibre-Hardcover-Plugin/1.0'
        }
        self._last_request = 0.0
        self._rate_limit_delay = rate_limit_delay
        self._requests_per_minute = 60  # API limit: 60 requests per minute

    def _throttle(self):
        """Rate limit API calls - 60 requests per minute max"""
        elapsed = time.time() - self._last_request
        min_delay = 60.0 / self._requests_per_minute  # 1 second between requests
        if elapsed < min_delay:
            time.sleep(min_delay - elapsed)

    def run_query(self, query: str, variables: Optional[dict] = None) -> dict:
        """Execute a GraphQL query with error handling and rate limiting"""
        self._throttle()
        
        payload = json.dumps({
            'query': query.strip(), 
            'variables': variables or {}
        }).encode('utf-8')
        
        req = urllib.request.Request(GRAPHQL_ENDPOINT, data=payload, headers=self.headers)
        
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._last_request = time.time()
                response_data = resp.read().decode('utf-8')
                data = json.loads(response_data)
                
        except urllib.error.HTTPError as e:
            error_msg = f"HTTP {e.code}: {e.reason}"
            if e.code == 401:
                error_msg += " - Invalid API token"
            elif e.code == 429:
                error_msg += " - Rate limit exceeded"
            raise Exception(error_msg)
            
        except urllib.error.URLError as e:
            raise Exception(f"Network error: {e.reason}")
            
        except json.JSONDecodeError as e:
            raise Exception(f"Invalid JSON response: {e}")
            
        except Exception as e:
            raise Exception(f"Request failed: {str(e)}")

        if 'errors' in data:
            error_details = data['errors']
            if isinstance(error_details, list) and error_details:
                error_msg = error_details[0].get('message', 'Unknown GraphQL error')
            else:
                error_msg = str(error_details)
            raise Exception(f"GraphQL error: {error_msg}")
            
        return data.get('data', {})

    def test_connection(self) -> bool:
        """Test if the API token is valid"""
        try:
            query = """
            query TestConnection {
                me {
                    id
                    username
                    email
                }
            }
            """
            result = self.run_query(query)

            me_data = result.get('me')
            if isinstance(me_data, list) and me_data:
                me_data = me_data[0]  # take the first user

            if isinstance(me_data, dict):
                if me_data.get('id') and (me_data.get('username') or me_data.get('email')):
                    return True

            print("API test failed: Unexpected response format or insufficient user information")
            return False

        except Exception as e:
            print(f"API Connection Error: {str(e)}")
            traceback.print_exc()
            return False


    def search_books_by_title(self, title: str) -> List[Dict]:
        """Search for books by title using Hardcover's actual schema"""
        query = """
        query SearchBooksByTitle($title: String!) {
            books(where: {title: {_ilike: $title}}, limit: 10) {
                id
                title
                slug
                release_date
                contributions {
                    author {
                        id
                        name
                    }
                }
                editions {
                    id
                    isbn_10
                    isbn_13
                    title
                    edition_format
                }
            }
        }
        """
        
        # Use ILIKE for case-insensitive search with wildcards
        search_title = f"%{title}%"
        variables = {'title': search_title}
        
        try:
            data = self.run_query(query, variables)
            return data.get('books', [])
        except Exception:
            return []

    def search_books_by_isbn(self, isbn: str) -> List[Dict]:
        """Search for books by ISBN"""
        query = """
        query SearchBooksByISBN($isbn10: String, $isbn13: String) {
            editions(where: {
                _or: [
                    {isbn_10: {_eq: $isbn10}},
                    {isbn_13: {_eq: $isbn13}}
                ]
            }, limit: 5) {
                id
                isbn_10
                isbn_13
                title
                book {
                    id
                    title
                    slug
                    contributions {
                        author {
                            id
                            name
                        }
                    }
                }
            }
        }
        """
        
        # Clean ISBN and try both formats
        clean_isbn = isbn.replace('-', '').replace(' ', '')
        isbn_10 = clean_isbn if len(clean_isbn) == 10 else None
        isbn_13 = clean_isbn if len(clean_isbn) == 13 else None
        
        variables = {'isbn10': isbn_10, 'isbn13': isbn_13}
        
        try:
            data = self.run_query(query, variables)
            editions = data.get('editions', [])
            # Convert editions to books format
            books = []
            for edition in editions:
                if edition.get('book'):
                    book = edition['book'].copy()
                    book['matching_edition'] = edition
                    books.append(book)
            return books
        except Exception:
            return []

    def get_owned_books_with_titles(self) -> Dict[str, Dict]:
        """Get user's owned books with titles for comparison (returns dict with book_id -> book_info)"""
        query = """
        query GetOwnedBooksWithTitles {
            user_books(where: {owned: {_eq: true}}) {
                book_id
                book {
                    id
                    title
                    slug
                    contributions {
                        author {
                            name
                        }
                    }
                    editions {
                        isbn_10
                        isbn_13
                    }
                }
            }
        }
        """
        
        try:
            data = self.run_query(query)
            user_books = data.get('user_books', [])
            owned_books = {}
            
            for user_book in user_books:
                book_data = user_book.get('book', {})
                book_id = str(book_data.get('id', ''))
                
                if book_id:
                    # Extract ISBNs from editions
                    isbns = set()
                    for edition in book_data.get('editions', []):
                        if edition.get('isbn_10'):
                            isbns.add(edition['isbn_10'])
                        if edition.get('isbn_13'):
                            isbns.add(edition['isbn_13'])
                    
                    # Extract author names
                    authors = []
                    for contrib in book_data.get('contributions', []):
                        if contrib.get('author', {}).get('name'):
                            authors.append(contrib['author']['name'])
                    
                    owned_books[book_id] = {
                        'id': book_id,
                        'title': book_data.get('title', '').lower().strip(),
                        'authors': authors,
                        'isbns': isbns,
                        'slug': book_data.get('slug', '')
                    }
            
            return owned_books
            
        except Exception:
            return {}

    def is_book_already_owned(self, title: str, authors: List[str], isbn: str, owned_books: Dict) -> Optional[str]:
        """Check if a book is already owned by comparing title, authors, and ISBN"""
        clean_title = title.lower().strip()
        clean_isbn = isbn.replace('-', '').replace(' ', '') if isbn else None
        
        for book_id, book_info in owned_books.items():
            # Check ISBN match first (most reliable)
            if clean_isbn and book_info['isbns']:
                if clean_isbn in book_info['isbns']:
                    return book_id
            
            # Check title match (fuzzy)
            if book_info['title'] and clean_title:
                # Simple fuzzy matching - exact match or one is contained in the other
                if (clean_title == book_info['title'] or 
                    clean_title in book_info['title'] or 
                    book_info['title'] in clean_title):
                    
                    # If title matches, also check if authors are similar
                    if authors and book_info['authors']:
                        # Simple author matching - check if any author names overlap
                        calibre_authors = [a.lower().strip() for a in authors]
                        hardcover_authors = [a.lower().strip() for a in book_info['authors']]
                        
                        for ca in calibre_authors:
                            for ha in hardcover_authors:
                                if ca in ha or ha in ca:
                                    return book_id
                    else:
                        # No authors to compare, title match is enough
                        return book_id
        
        return None

    def add_book_to_owned(self, book_id: str) -> bool:
        """Add a book to user's owned list (without reading status)"""
        mutation = """
        mutation AddBookToOwned($book_id: bigint!) {
            insert_user_books_one(object: {
                book_id: $book_id,
                owned: true
            }, on_conflict: {
                constraint: user_books_pkey,
                update_columns: [owned]
            }) {
                id
                book_id
                owned
            }
        }
        """
        
        variables = {
            'book_id': int(book_id)
        }
        
        try:
            data = self.run_query(mutation, variables)
            return data.get('insert_user_books_one') is not None
        except Exception:
            return False

    def create_book_on_hardcover(self, title: str, authors: List[str], metadata) -> Optional[str]:
        """Create a new book on Hardcover with metadata"""
        mutation = """
        mutation CreateBook($title: String!, $subtitle: String, $description: String, 
                          $release_date: date, $isbn_10: String, $isbn_13: String, 
                          $publisher: String, $pages: Int) {
            insert_books_one(object: {
                title: $title,
                subtitle: $subtitle,
                description: $description,
                release_date: $release_date,
                publisher: $publisher,
                pages: $pages,
                editions: {
                    data: [{
                        title: $title,
                        isbn_10: $isbn_10,
                        isbn_13: $isbn_13,
                        pages: $pages,
                        publisher: $publisher
                    }]
                }
            }) {
                id
                title
            }
        }
        """
        
        # Extract metadata
        subtitle = getattr(metadata, 'subtitle', None) if hasattr(metadata, 'subtitle') else None
        description = getattr(metadata, 'comments', None)
        publisher = getattr(metadata, 'publisher', None)
        pages = getattr(metadata, 'pages', None) if hasattr(metadata, 'pages') else None
        
        # Handle publication date
        release_date = None
        if hasattr(metadata, 'pubdate') and metadata.pubdate:
            try:
                release_date = str(metadata.pubdate.date())
            except Exception:
                try:
                    release_date = str(metadata.pubdate)[:10]  # Take first 10 chars (YYYY-MM-DD)
                except Exception:
                    pass
        
        # Handle ISBN
        isbn_10 = None
        isbn_13 = None
        if hasattr(metadata, 'identifiers') and metadata.identifiers:
            isbn = metadata.identifiers.get('isbn', '')
            if isbn:
                clean_isbn = isbn.replace('-', '').replace(' ', '')
                if len(clean_isbn) == 10:
                    isbn_10 = clean_isbn
                elif len(clean_isbn) == 13:
                    isbn_13 = clean_isbn
        
        variables = {
            'title': title,
            'subtitle': subtitle,
            'description': description,
            'release_date': release_date,
            'isbn_10': isbn_10,
            'isbn_13': isbn_13,
            'publisher': publisher,
            'pages': pages
        }
        
        try:
            data = self.run_query(mutation, variables)
            created_book = data.get('insert_books_one')
            if created_book:
                return str(created_book['id'])
        except Exception as e:
            print(f"Failed to create book '{title}': {str(e)}")
        
        return None

# --- Sync Job ---
class SyncJob(ThreadedJob):
    """Background job for syncing books to Hardcover owned list"""
    
    def __init__(self, api: HardcoverAPI, db, book_ids: List[int]):
        ThreadedJob.__init__(self, 'Hardcover Sync', lambda x: x, lambda x, y: x)
        self.api = api
        self.db = db
        self.book_ids = book_ids
        self.results = {
            'added_to_owned': 0,
            'skipped_already_owned': 0,
            'created_new_books': 0,
            'existing_owned_count': 0,
            'errors': 0,
            'error_details': []
        }
        self.status_message = "Starting sync..."

    def run(self):
        """Execute the sync job - efficiently add Calibre books to Hardcover owned list"""
        try:
            # STEP 1: Get user's existing owned books with full details for comparison
            self.status_message = "Fetching your current Hardcover library..."
            try:
                owned_books = self.api.get_owned_books_with_titles()
                self.results['existing_owned_count'] = len(owned_books)
            except Exception as e:
                self.results['error_details'].append(f"Failed to get owned books: {str(e)}")
                owned_books = {}
            
            total_books = len(self.book_ids)
            books_to_process = []  # Books that need API calls
            
            # STEP 2: Compare Calibre books against owned books to minimize API calls
            self.status_message = "Comparing with your Calibre library..."
            for i, book_id in enumerate(self.book_ids):
                try:
                    # Update progress for comparison phase
                    progress = int((i / total_books) * 20)  # Use first 20% for comparison
                    self.percent = progress
                    
                    # Get book metadata from Calibre
                    try:
                        metadata = self.db.get_metadata(book_id, index_is_id=True)
                    except Exception as e:
                        self.results['errors'] += 1
                        self.results['error_details'].append(f"Failed to read book {book_id}: {str(e)}")
                        continue
                    
                    # Extract book info
                    title = metadata.title or 'Unknown Title'
                    authors = list(metadata.authors) if metadata.authors else []
                    isbn = None
                    
                    # Try to get ISBN from identifiers
                    if hasattr(metadata, 'identifiers') and metadata.identifiers:
                        isbn = metadata.identifiers.get('isbn', None)
                    
                    # Check if already owned (no API call needed)
                    existing_book_id = self.api.is_book_already_owned(title, authors, isbn, owned_books)
                    if existing_book_id:
                        self.results['skipped_already_owned'] += 1
                        continue
                    
                    # Add to processing list
                    books_to_process.append({
                        'calibre_id': book_id,
                        'metadata': metadata,
                        'title': title,
                        'authors': authors,
                        'isbn': isbn
                    })
                    
                except Exception as e:
                    self.results['errors'] += 1
                    self.results['error_details'].append(f"Error comparing book {book_id}: {str(e)}")
                    continue
            
            # STEP 3: Process remaining books (search/create/add to owned)
            self.status_message = "Processing new books..."
            for i, book_info in enumerate(books_to_process):
                try:
                    # Update progress for processing phase (20-100%)
                    progress = 20 + int((i / len(books_to_process)) * 80) if books_to_process else 100
                    self.percent = progress
                    
                    title = book_info['title']
                    authors = book_info['authors']
                    isbn = book_info['isbn']
                    metadata = book_info['metadata']
                    
                    # Search for book on Hardcover - try ISBN first, then title
                    search_results = []
                    if isbn:
                        search_results = self.api.search_books_by_isbn(isbn)
                    
                    if not search_results:
                        search_results = self.api.search_books_by_title(title)
                    
                    hardcover_book_id = None
                    
                    if search_results:
                        # Book exists on Hardcover
                        hardcover_book = search_results[0]
                        hardcover_book_id = str(hardcover_book['id'])
                    else:
                        # Book doesn't exist on Hardcover - create it
                        try:
                            hardcover_book_id = self.api.create_book_on_hardcover(title, authors, metadata)
                            if hardcover_book_id:
                                self.results['created_new_books'] += 1
                            else:
                                self.results['errors'] += 1
                                self.results['error_details'].append(f"Failed to create book: {title}")
                                continue
                        except Exception as e:
                            self.results['errors'] += 1
                            self.results['error_details'].append(f"Error creating '{title}': {str(e)}")
                            continue
                    
                    # Add to owned list
                    if hardcover_book_id:
                        success = self.api.add_book_to_owned(hardcover_book_id)
                        
                        if success:
                            self.results['added_to_owned'] += 1
                            # Update local owned_books cache to avoid future duplicates
                            owned_books[hardcover_book_id] = {
                                'id': hardcover_book_id,
                                'title': title.lower().strip(),
                                'authors': authors,
                                'isbns': {isbn} if isbn else set(),
                                'slug': ''
                            }
                        else:
                            self.results['errors'] += 1
                            self.results['error_details'].append(f"Failed to add '{title}' to owned list")
                        
                except Exception as e:
                    self.results['errors'] += 1
                    self.results['error_details'].append(f"Error processing '{title}': {str(e)}")
                    continue
            
            self.percent = 100
            self.status_message = "Sync completed!"
            
        except Exception as e:
            self.results['error_details'].append(f"Sync job failed: {str(e)}")
            raise

# --- Configuration Widget ---
class HardcoverConfigWidget(ConfigWidgetBase):
    def __init__(self, *args, **kwargs):
        # Ensure compatibility with different Calibre versions
        super().__init__(*args, **kwargs)
        self.gui = None
        
        # Create main widget and layout
        self.setLayout(QVBoxLayout())
        
        # Instructions
        instructions = QTextEdit()
        instructions.setReadOnly(True)
        instructions.setMaximumHeight(100)
        instructions.setPlainText(
            "Get your API token from Hardcover.app → Account Settings → Hardcover API.\n"
            "This plugin will add all your Calibre books to your Hardcover 'Owned' list.\n"
            "Books that don't exist on Hardcover will be created with your Calibre metadata."
        )
        
        # API Token
        token_layout = QFormLayout()
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        token_layout.addRow(QLabel('Hardcover API Token:'), self.token_edit)
        
        # Test connection button
        test_layout = QHBoxLayout()
        self.test_btn = QPushButton('Test Connection')
        self.test_btn.clicked.connect(self.test_connection)
        test_layout.addWidget(self.test_btn)
        test_layout.addStretch()
        
        # Options
        self.sync_owned_checkbox = QCheckBox('Only sync books not already in Hardcover library')
        
        # Rate limiting
        rate_layout = QFormLayout()
        self.rate_limit_edit = QLineEdit()
        rate_layout.addRow(QLabel('Rate limit delay (seconds):'), self.rate_limit_edit)
        
        # Add all widgets to layout
        self.layout().addWidget(instructions)
        self.layout().addLayout(token_layout)
        self.layout().addLayout(test_layout)
        self.layout().addWidget(self.sync_owned_checkbox)
        self.layout().addLayout(rate_layout)
        
        # Initialize with current preferences
        self.initialize()

    def genesis(self, gui):
        # This method is kept for compatibility, but initialization is now done in __init__
        self.gui = gui

    def widget(self):
        # Ensure a widget is returned for compatibility
        return self

    def test_connection(self):
        token = self.token_edit.text().strip()
        if not token:
            QMessageBox.warning(self, 'Warning', 'Please enter an API token first.')
            return
            
        try:
            api = HardcoverAPI(token)
            success = api.test_connection()
            if success:
                QMessageBox.information(self, 'Success', 'Connection successful!')
            else:
                QMessageBox.warning(self, 'Failed', 'Connection failed. Check your token.')
        except Exception as e:
            QMessageBox.critical(self, 'Error', f'Connection test failed: {str(e)}')

    def initialize(self):
        # Initialize widget with saved preferences
        self.token_edit.setText(prefs.get('api_token', ''))
        self.sync_owned_checkbox.setChecked(prefs.get('sync_owned_only', False))
        self.rate_limit_edit.setText(str(prefs.get('rate_limit_delay', 1.1)))

    def commit(self):
        # Save preferences
        prefs['api_token'] = self.token_edit.text().strip()
        prefs['sync_owned_only'] = self.sync_owned_checkbox.isChecked()
        try:
            prefs['rate_limit_delay'] = float(self.rate_limit_edit.text())
        except ValueError:
            prefs['rate_limit_delay'] = 1.1
        return True

    def save_settings(self):
        # Explicitly implement save_settings to resolve NotImplementedError
        return self.commit()

    def restore_defaults(self):
        # Restore default settings
        self.token_edit.setText('')
        self.sync_owned_checkbox.setChecked(False)
        self.rate_limit_edit.setText('1.1')
        return True

    def initialize(self):
        ConfigWidgetBase.initialize(self)
        self.token_edit.setText(prefs['api_token'])
        self.sync_owned_checkbox.setChecked(prefs['sync_owned_only'])
        self.rate_limit_edit.setText(str(prefs['rate_limit_delay']))
        return True

    def restore_defaults(self):
        #ConfigWidgetBase.restore_defaults(self)
        self.token_edit.setText('')
        self.sync_owned_checkbox.setChecked(False)
        self.rate_limit_edit.setText('1.1')
        return True

    def commit(self):
        prefs['api_token'] = self.token_edit.text().strip()
        prefs['sync_owned_only'] = self.sync_owned_checkbox.isChecked()
        try:
            prefs['rate_limit_delay'] = float(self.rate_limit_edit.text())
        except ValueError:
            prefs['rate_limit_delay'] = 1.1
        #return ConfigWidgetBase.commit(self)
        return True

    def test_connection(self):
        token = self.token_edit.text().strip()
        if not token:
            QMessageBox.warning(self, 'Warning', 'Please enter an API token first.')
            return
            
        self.test_btn.setEnabled(False)
        self.test_btn.setText('Testing...')
        
        try:
            api = HardcoverAPI(token)
            success = api.test_connection()
            if success:
                QMessageBox.information(self, 'Success', 'Connection successful!')
            else:
                QMessageBox.warning(self, 'Failed', 'Connection failed. Check your token.')
        except Exception as e:
            QMessageBox.critical(self, 'Error', f'Connection test failed: {str(e)}')
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.setText('Test Connection')

# --- Sync Dialog ---
class SyncDialog(QDialog):
    def __init__(self, api: HardcoverAPI, db, book_ids: List[int], parent=None):
        super().__init__(parent)
        self.api = api
        self.db = db
        self.book_ids = book_ids
        self.job = None
        
        self.setWindowTitle('Hardcover Sync')
        self.setModal(True)
        self.resize(400, 150)
        
        layout = QVBoxLayout(self)
        
        # Question
        question_label = QLabel('Do you want to sync with Hardcover.app?')
        question_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(question_label)
        
        # Info
        info_text = f'This will add {len(book_ids)} books from your Calibre library to your Hardcover "Owned" list.'
        layout.addWidget(QLabel(info_text))
        
        # Progress (hidden initially)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        
        self.status_label = QLabel('')
        layout.addWidget(self.status_label)
        
        # Buttons
        button_layout = QHBoxLayout()
        self.sync_btn = QPushButton('Sync')
        self.sync_btn.clicked.connect(self.start_sync)
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(self.sync_btn)
        button_layout.addWidget(self.cancel_btn)
        layout.addLayout(button_layout)

    def start_sync(self):
        self.sync_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.status_label.setText('Starting sync...')
        
        # Create and start sync job
        self.job = SyncJob(self.api, self.db, self.book_ids)
        self.job.daemon = True
        
        # Monitor job progress
        def check_progress():
            if self.job and self.job.is_alive():
                progress = getattr(self.job, 'percent', 0)
                status = getattr(self.job, 'status_message', 'Syncing...')
                self.progress.setValue(progress)
                self.status_label.setText(f'{status} {progress}%')
                threading.Timer(0.5, check_progress).start()
            else:
                self.sync_finished()
        
        self.job.start()
        check_progress()

    def sync_finished(self):
        if not self.job:
            return
            
        self.progress.setValue(100)
        self.status_label.setText('Sync completed!')
        results = getattr(self.job, 'results', {})
        
        added = results.get('added_to_owned', 0)
        skipped = results.get('skipped_already_owned', 0)
        created = results.get('created_new_books', 0)
        existing = results.get('existing_owned_count', 0)
        errors = results.get('errors', 0)
        error_details = results.get('error_details', [])
        
        total_calibre = len(self.book_ids)
        
        message = f"Sync completed!\n\n"
        message += f"📚 Your Calibre Library: {total_calibre} books\n"
        message += f"📖 Already Owned on Hardcover: {existing} books\n\n"
        message += f"✅ Added to Owned: {added}\n"
        message += f"⚪ Skipped (already owned): {skipped}\n" 
        message += f"✨ New Books Created: {created}\n"
        message += f"❌ Errors: {errors}"
        
        if error_details:
            message += f"\n\nFirst few errors:\n" + "\n".join(error_details[:3])
            if len(error_details) > 3:
                message += f"\n... and {len(error_details) - 3} more"
        
        QMessageBox.information(self, 'Sync Complete', message)
        self.accept()

# --- Main Plugin Interface Action ---
class HardcoverSyncAction(InterfaceAction):

    def __init__(self, gui, site_customization=None):
        # Add site_customization parameter with a default of None
        InterfaceAction.__init__(self, gui, site_customization)

    def initialization_complete(self):
        # This gets called when the action is fully loaded
        print("Hardcover Sync plugin loaded successfully")
    
    name = 'Hardcover Sync'
    action_spec = ('Hardcover Sync', None, 'Sync your Calibre library with Hardcover.app', None)
    
    def genesis(self):
        # Set up the action icon - use a built-in icon or None
        icon = self.load_resources(['images/icon.png']).get('images/icon.png')
        self.qaction.setIcon(icon) if icon else None
        self.qaction.triggered.connect(self.sync_library)
        
        # Ensure the action is added to the GUI
        self.gui.addAction(self.qaction)

    def is_multiple_books_action(self):
        """Enable action for multiple book selections"""
        return True

    def get_library_action_names(self, mi):
        """Ensure the action appears in the context menu"""
        return [_('Hardcover Sync')]

    def perform_library_action(self, action_name, mi):
        """Handle the context menu action"""
        if action_name == _('Hardcover Sync'):
            self.sync_library()

    def sync_library(self, book_ids=None):
        # Check if token is configured
        token = prefs['api_token'].strip()
        if not token:
            if QMessageBox.question(
                self.gui,
                'Configure Plugin',
                'No Hardcover API token configured. Open plugin settings?',
                QMessageBox.Yes | QMessageBox.No
            ) == QMessageBox.Yes:
                self.interface_action_base_plugin.do_user_config(self.gui)
            return
        
        # Get books to sync - either selected or all
        try:
            if book_ids is None:
                book_ids = list(self.gui.current_db.get_selected_ids())
            
            if not book_ids:
                book_ids = list(self.gui.current_db.all_ids())
        except Exception as e:
            error_dialog(self.gui, 'Error', f'Failed to get book list: {str(e)}', show=True)
            return
        
        if not book_ids:
            info_dialog(self.gui, 'No Books', 'No books found in your Calibre library.', show=True)
            return
        
        # Create API instance
        try:
            api = HardcoverAPI(token, prefs.get('rate_limit_delay', 1.1))
        except Exception as e:
            error_dialog(self.gui, 'Error', f'Failed to initialize API: {str(e)}', show=True)
            return

        def config_widget(self):
            return HardcoverConfigWidget()
        
        # Show sync dialog
        dialog = SyncDialog(api, self.gui.current_db, book_ids, self.gui)
        dialog.exec_()

        def load_settings(self, config_widget):
            if hasattr(config_widget, 'initialize'):
                return config_widget.initialize()
            return True

        def save_settings(self, config_widget):
            if hasattr(config_widget, 'commit'):
                return config_widget.commit()
            return True


# --- Plugin Base Class for Calibre ---
class HardcoverSyncPlugin(InterfaceActionBase):
    
    name = PLUGIN_NAME
    description = 'Sync selected or all books from your Calibre library to Hardcover.app owned list'
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Hardcover Sync Plugin'
    version = (1, 1, 1)  # Bumped version
    minimum_calibre_version = (6, 0, 0)
    
    actual_plugin = HardcoverSyncAction
    
    def is_customizable(self):
        return True
    
    def config_widget(self):
        return HardcoverConfigWidget()
    
    def cli_main(self, args):
        # Command line interface (optional)
        print('Hardcover Sync Plugin - use via Calibre GUI')
        return 0
    
    def load_actual_plugin(self, gui):
        # Explicitly load the plugin
        return self.actual_plugin(gui)
    
    def save_settings(self, config_widget):
        # Explicitly implement save_settings method
        try:
            # Try to use the widget's commit method if available
            if hasattr(config_widget, 'commit'):
                return config_widget.commit()
            
            # Fallback to saving preferences directly
            prefs['api_token'] = config_widget.token_edit.text().strip()
            prefs['sync_owned_only'] = config_widget.sync_owned_checkbox.isChecked()
            try:
                prefs['rate_limit_delay'] = float(config_widget.rate_limit_edit.text())
            except ValueError:
                prefs['rate_limit_delay'] = 1.1
            
            return True
        except Exception as e:
            print(f"Error saving settings: {e}")
            return False

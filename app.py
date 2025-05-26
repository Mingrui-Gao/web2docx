import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import convertapi
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
import tempfile
import shutil
import logging

# Configure ConvertAPI
convertapi.api_credentials = st.secrets["CONVERTAPI_SECRET"]

# Semaphore to limit concurrent API calls
api_semaphore = Semaphore(20)

# Configure logging to suppress some warnings
logging.getLogger('streamlit').setLevel(logging.ERROR)

def get_product_links(url):
    """Extract all product links from the collection page"""
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        product_links = set()
        
        for link in soup.find_all('a', href=True):
            href = link['href']
            full_url = urljoin(url, href)
            parsed_url = urlparse(full_url)
            
            # Check if it's a product URL from the same domain
            if parsed_url.netloc == urlparse(url).netloc and '/products/' in parsed_url.path:
                product_links.add(full_url)
        
        return sorted(list(product_links))
    
    except requests.RequestException as e:
        return []

def convert_url_to_pdf(url, output_dir, index):
    """Convert a single URL to PDF using ConvertAPI - NO Streamlit calls in threads"""
    with api_semaphore:
        try:
            product_name = url.split('/')[-1]
            filename = f"{index:03d}_{product_name}.pdf"
            filepath = os.path.join(output_dir, filename)
            
            # Test the URL first
            test_response = requests.head(url, timeout=10)
            if test_response.status_code != 200:
                return {'success': False, 'error': f'URL not accessible (status: {test_response.status_code})', 'index': index}
            
            result = convertapi.convert('pdf', {
                'Url': url,
                'PageSize': 'a4',
                'MarginTop': '10',
                'MarginBottom': '10',
                'MarginLeft': '10',
                'MarginRight': '10',
                'Timeout': 30
            }, from_format='web')
            
            result.file.save(filepath)
            return {'success': True, 'path': filepath, 'index': index, 'url': url}
            
        except Exception as e:
            return {'success': False, 'error': str(e), 'index': index, 'url': url}

def merge_pdfs(pdf_files, output_filename):
    """Merge all PDFs into one using ConvertAPI"""
    try:
        # Verify all files exist
        for pdf_file in pdf_files:
            if not os.path.exists(pdf_file):
                raise FileNotFoundError(f"PDF file not found: {pdf_file}")
        
        result = convertapi.convert('merge', {
            'Files': pdf_files
        }, from_format='pdf')
        
        result.file.save(output_filename)
        return output_filename
        
    except Exception as e:
        return None

def convert_pdf_to_docx(pdf_file, output_filename):
    """Convert PDF to Word DOCX using ConvertAPI"""
    try:
        if not os.path.exists(pdf_file):
            raise FileNotFoundError(f"PDF file not found: {pdf_file}")
            
        result = convertapi.convert('docx', {
            'File': pdf_file
        }, from_format='pdf')
        
        result.file.save(output_filename)
        return output_filename
        
    except Exception as e:
        return None

def process_collection(url, progress_bar, status_text):
    """Process entire collection and return the Word document"""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Get product links
        status_text.text("Fetching product links...")
        product_links = get_product_links(url)
        
        if not product_links:
            st.error("No product links found!")
            return None
        
        status_text.text(f"Found {len(product_links)} products. Starting conversion...")
        
        # Convert URLs to PDFs concurrently with reduced thread count
        pdf_files = []
        failed_conversions = []
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            future_to_url = {
                executor.submit(convert_url_to_pdf, url, temp_dir, i): (url, i) 
                for i, url in enumerate(product_links, 1)
            }
            
            completed = 0
            for future in as_completed(future_to_url):
                url_orig, index = future_to_url[future]
                try:
                    result = future.result()
                    if result['success']:
                        pdf_files.append((result['index'], result['path']))
                        status_text.text(f"✅ Converted product {result['index']}/{len(product_links)}")
                    else:
                        failed_conversions.append(f"Product {result['index']}: {result['error']}")
                        status_text.text(f"❌ Failed product {result['index']}/{len(product_links)}")
                    
                    completed += 1
                    progress_bar.progress(completed / len(product_links))
                    
                except Exception as e:
                    failed_conversions.append(f"Product {index}: {str(e)}")
        
        # Show conversion results
        if failed_conversions:
            with st.expander(f"⚠️ {len(failed_conversions)} conversion failures"):
                for failure in failed_conversions[:10]:
                    st.text(failure)
                if len(failed_conversions) > 10:
                    st.text(f"... and {len(failed_conversions) - 10} more")
        
        if not pdf_files:
            st.error("No PDFs were successfully created! Check the URLs and API credentials.")
            return None
        
        st.success(f"Successfully converted {len(pdf_files)} out of {len(product_links)} products")
        
        # Sort PDFs by index
        pdf_files.sort(key=lambda x: x[0])
        pdf_paths = [path for _, path in pdf_files]
        
        # Merge PDFs
        status_text.text("Merging PDFs...")
        merged_pdf = os.path.join(temp_dir, "merged.pdf")
        if not merge_pdfs(pdf_paths, merged_pdf):
            st.error("Failed to merge PDFs")
            return None
        
        # Convert to DOCX
        status_text.text("Converting to Word document...")
        final_docx = os.path.join(temp_dir, "products.docx")
        if not convert_pdf_to_docx(merged_pdf, final_docx):
            st.error("Failed to convert to Word document")
            return None
        
        # Read the file to return it
        with open(final_docx, 'rb') as f:
            return f.read()

# Streamlit UI
st.set_page_config(page_title="Product Collection Scraper", page_icon="📄")

st.title("🛍️ Product Collection to Word Document")
st.markdown("Convert any product collection page to a Word document")

# Initialize session state for URL
if 'selected_url' not in st.session_state:
    st.session_state.selected_url = ""

# Example URLs
example_urls = [
    "https://havnstore.com/collections/satisfy",
    "https://havnstore.com/collections/new-arrivals",
    "https://havnstore.com/collections/all"
]

# Add example URL buttons
st.markdown("**Example URLs:**")
col1, col2, col3 = st.columns(3)
with col1:
    if st.button("Use Example 1", key="btn1"):
        st.session_state.selected_url = example_urls[0]
with col2:
    if st.button("Use Example 2", key="btn2"):
        st.session_state.selected_url = example_urls[1]
with col3:
    if st.button("Use Example 3", key="btn3"):
        st.session_state.selected_url = example_urls[2]

# Input field - use session state value
url = st.text_input(
    "Enter collection URL:",
    value=st.session_state.selected_url,
    placeholder="https://havnstore.com/collections/satisfy",
    help="Enter the URL of a product collection page"
)

# Update session state when URL changes
if url != st.session_state.selected_url:
    st.session_state.selected_url = url

# Convert button
if st.button("🚀 Convert to Word Document", type="primary"):
    if url:
        start_time = time.time()
        
        # Create progress indicators
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Process the collection
        with st.spinner("Processing..."):
            docx_content = process_collection(url, progress_bar, status_text)
        
        if docx_content:
            elapsed_time = time.time() - start_time
            status_text.text(f"✅ Completed in {elapsed_time:.1f} seconds!")
            
            # Extract domain name for filename
            domain = urlparse(url).netloc.replace('www.', '').replace('.com', '')
            collection_name = url.split('/')[-1]
            filename = f"{domain}_{collection_name}_products.docx"
            
            # Provide download button
            st.download_button(
                label="📥 Download Word Document",
                data=docx_content,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
        else:
            st.error("Conversion failed. Please check the error messages above.")
    else:
        st.warning("Please enter a URL first!")

# Instructions
with st.expander("ℹ️ How to use"):
    st.markdown("""
    1. Click on an example URL or enter your own collection URL
    2. Click "Convert to Word Document"
    3. Wait for the process to complete
    4. Download your Word document
    
    **Note:** The process may take a few minutes depending on the number of products.
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using ConvertAPI")
import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import convertapi
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
import tempfile
import logging

# Configure ConvertAPI
convertapi.api_credentials = st.secrets["CONVERTAPI_SECRET"]

# Semaphore to limit concurrent API calls
api_semaphore = Semaphore(20)

# Configure logging to suppress some warnings
logging.getLogger('streamlit').setLevel(logging.ERROR)

def detect_product_links(url, soup):
    """Detect product links based on common patterns"""
    parsed_url = urlparse(url)
    domain = parsed_url.netloc
    product_links = set()
    
    # Strategy 1: Look for common product URL patterns
    product_patterns = [
        '/products/',
        '/product/',
        '/item/',
        '/goods/',
        'item_detail.php',
        'product_detail.php',
        'goods_view.php',
        'shop_detail.php'
    ]
    
    # Strategy 2: For Korean sites, look for specific parameters
    korean_patterns = ['bno', 'no', 'idx', 'product_no', 'goods_no', 'item_no']
    
    for link in soup.find_all('a', href=True):
        href = link['href']
        full_url = urljoin(url, href)
        parsed_link = urlparse(full_url)
        
        # Skip if different domain
        if parsed_link.netloc and parsed_link.netloc != domain:
            continue
            
        # Check for product patterns in URL
        for pattern in product_patterns:
            if pattern in full_url:
                product_links.add(full_url)
                break
        
        # Check for Korean site patterns (query parameters)
        query_params = parse_qs(parsed_link.query)
        for param in korean_patterns:
            if param in query_params:
                product_links.add(full_url)
                break
    
    # If no products found, try to find links with images inside them
    if not product_links:
        st.info("No standard product links found. Looking for alternative patterns...")
        
        # Look for links containing product images
        for link in soup.find_all('a', href=True):
            if link.find('img') and ('product' in str(link).lower() or 'item' in str(link).lower()):
                full_url = urljoin(url, link['href'])
                if urlparse(full_url).netloc == domain:
                    product_links.add(full_url)
        
        # Look for links in common product containers
        for container in soup.find_all(['div', 'li', 'article'], class_=lambda x: x and any(
            keyword in str(x).lower() for keyword in ['product', 'item', 'goods', '상품'])):
            for link in container.find_all('a', href=True):
                full_url = urljoin(url, link['href'])
                if urlparse(full_url).netloc == domain and full_url != url:
                    product_links.add(full_url)
    
    return sorted(list(product_links))

def get_product_links(url):
    """Extract all product links from the collection page"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        # Handle encoding for Korean sites
        if 'euc-kr' in response.headers.get('Content-Type', '').lower():
            response.encoding = 'euc-kr'
        elif not response.encoding:
            response.encoding = 'utf-8'
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Try to detect product links
        product_links = detect_product_links(url, soup)
        
        # Show debug info if requested
        if st.checkbox("Show debug info"):
            st.write(f"Found {len(product_links)} product links")
            with st.expander("View detected links"):
                for link in product_links[:10]:  # Show first 10
                    st.code(link)
        
        return product_links
    
    except requests.RequestException as e:
        st.error(f"Error fetching page: {e}")
        return []

def convert_url_to_pdf(url, output_dir, index):
    """Convert a single URL to PDF using ConvertAPI - NO Streamlit calls in threads"""
    with api_semaphore:
        try:
            # Clean filename
            filename = f"{index:03d}_product.pdf"
            filepath = os.path.join(output_dir, filename)
            
            result = convertapi.convert('pdf', {
                'Url': url,
                'PageSize': 'a4',
                'MarginTop': '10',
                'MarginBottom': '10',
                'MarginLeft': '10',
                'MarginRight': '10',
                'LoadLazyContent': 'true',  # Important for dynamic content
                'WaitTime': '3'  # Wait for page to load
            }, from_format='web')
            
            result.file.save(filepath)
            return {'success': True, 'path': filepath, 'index': index, 'url': url}
            
        except Exception as e:
            return {'success': False, 'error': str(e), 'index': index, 'url': url}

def merge_pdfs(pdf_files, output_filename):
    """Merge all PDFs into one using ConvertAPI"""
    try:
        result = convertapi.convert('merge', {
            'Files': pdf_files
        }, from_format='pdf')
        
        result.file.save(output_filename)
        return output_filename
        
    except Exception as e:
        st.error(f"Error merging PDFs: {e}")
        return None

def convert_pdf_to_docx(pdf_file, output_filename):
    """Convert PDF to Word DOCX using ConvertAPI"""
    try:
        result = convertapi.convert('docx', {
            'File': pdf_file
        }, from_format='pdf')
        
        result.file.save(output_filename)
        return output_filename
        
    except Exception as e:
        st.error(f"Error converting PDF to DOCX: {e}")
        return None

def process_collection(url, progress_bar, status_text, max_products=50):
    """Process entire collection and return the Word document"""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Get product links
        status_text.text("Fetching product links...")
        product_links = get_product_links(url)
        
        if not product_links:
            st.error("No product links found!")
            return None
        
        # Limit products if needed
        if len(product_links) > max_products:
            st.warning(f"Found {len(product_links)} products. Processing only first {max_products}.")
            product_links = product_links[:max_products]
        
        status_text.text(f"Found {len(product_links)} products. Starting conversion...")
        
        # Convert URLs to PDFs concurrently
        pdf_files = []
        failed_conversions = []
        
        with ThreadPoolExecutor(max_workers=10) as executor:
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
            st.error("No PDFs were successfully created!")
            return None
        
        st.success(f"Successfully converted {len(pdf_files)} out of {len(product_links)} products")
        
        # Sort PDFs by index
        pdf_files.sort(key=lambda x: x[0])
        pdf_paths = [path for _, path in pdf_files]
        
        # Merge PDFs
        status_text.text("Merging PDFs...")
        merged_pdf = os.path.join(temp_dir, "merged.pdf")
        if not merge_pdfs(pdf_paths, merged_pdf):
            return None
        
        # Convert to DOCX
        status_text.text("Converting to Word document...")
        final_docx = os.path.join(temp_dir, "products.docx")
        if not convert_pdf_to_docx(merged_pdf, final_docx):
            return None
        
        # Read the file to return it
        with open(final_docx, 'rb') as f:
            return f.read()

# Streamlit UI
st.set_page_config(page_title="Product Collection Scraper", page_icon="📄")

st.title("🛍️ Product Collection to Word Document")
st.markdown("Convert any product collection page to a Word document")

# Input field
url = st.text_input(
    "Enter collection/category URL:",
    placeholder="https://example.com/collections/products",
    help="Enter the URL of a product collection, category, or listing page"
)

# Add example URLs including Korean sites
st.markdown("**Example URLs:**")
col1, col2 = st.columns(2)

with col1:
    st.markdown("**International:**")
    if st.button("Satisfy Collection"):
        url = "https://havnstore.com/collections/satisfy"
    if st.button("HAVN New Arrivals"):
        url = "https://havnstore.com/collections/new-arrivals"

with col2:
    st.markdown("**Korean Sites:**")
    if st.button("Malbon Golf"):
        url = "https://malbongolfkorea.com/shop/big_section.php?cno1=1573"

# Advanced options
with st.expander("⚙️ Advanced Options"):
    max_products = st.number_input("Maximum products to convert", min_value=1, max_value=100, value=50)
    st.markdown("**Note:** Limiting products can help with large collections")

# Convert button
if st.button("🚀 Convert to Word Document", type="primary"):
    if url:
        start_time = time.time()
        
        # Create progress indicators
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Process the collection
        with st.spinner("Processing..."):
            docx_content = process_collection(url, progress_bar, status_text, max_products)
        
        if docx_content:
            elapsed_time = time.time() - start_time
            status_text.text(f"✅ Completed in {elapsed_time:.1f} seconds!")
            
            # Generate filename
            domain = urlparse(url).netloc.replace('www.', '').split('.')[0]
            filename = f"{domain}_products_{time.strftime('%Y%m%d_%H%M%S')}.docx"
            
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
    1. Enter a product collection/category URL
    2. Click "Convert to Word Document"
    3. Wait for the process to complete
    4. Download your Word document
    
    **Supported sites:**
    - Shopify stores (products in /collections/)
    - Korean shopping sites (various URL patterns)
    - Most e-commerce sites with product listings
    
    **Tips:**
    - Use the debug checkbox to see detected links
    - Limit max products for large collections
    - The process may take a few minutes depending on the number of products
    """)

# Footer
st.markdown("---")
st.markdown("Made with ❤️ using ConvertAPI")

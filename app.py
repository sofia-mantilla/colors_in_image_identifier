import os
from flask import Flask, render_template, request
from PIL import Image
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend for saving plots
import matplotlib.pyplot as plt
from collections import Counter
from sklearn.cluster import KMeans
from colormath.color_objects import sRGBColor, LabColor
from colormath.color_conversions import convert_color
import io
import base64

app = Flask(__name__)

# Ensure the upload folder exists
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


def rgb_to_lab(rgb):
    srgb = sRGBColor(rgb[0], rgb[1], rgb[2], is_upscaled=True)
    lab = convert_color(srgb, LabColor)
    return [lab.lab_l, lab.lab_a, lab.lab_b]


def analyze_image(image_path):
    # Load image
    img = Image.open(image_path)
    if img.mode != 'RGB':
        img = img.convert('RGB')

    # --- Keep a copy of the original for display ---
    img_array = np.array(img)
    print(f"Original image loaded: {img_array.shape}")

    # --- Create a resized copy for clustering to save memory ---
    img_small = img.copy()
    MAX_SIZE = (800, 800)  # max width, height in pixels
    img_small.thumbnail(MAX_SIZE, Image.LANCZOS)

    small_array = np.array(img_small)
    print(f"Resized image for KMeans: {small_array.shape}")

    # Flatten pixels for KMeans
    pixels = small_array.reshape(-1, 3)

    # If the image is very small, reduce clusters
    n_colors = min(20, len(pixels))
    print(f"Using {n_colors} clusters")

    # Detect dominant colors on the small image
    kmeans = KMeans(n_clusters=n_colors, random_state=42, n_init=10)
    kmeans.fit(pixels)

    centers = kmeans.cluster_centers_
    labels = kmeans.labels_
    color_counts = Counter(labels)
    total_pixels = len(labels)

    all_colors = []
    for i in range(n_colors):
        count = color_counts[i]
        percentage = (count / total_pixels) * 100
        if percentage > 0.5:  # keep small threshold
            color_rgb = tuple(map(int, centers[i]))
            hex_color = '#{:02x}{:02x}{:02x}'.format(*color_rgb)

            all_colors.append({
                'rgb': color_rgb,
                'hex': hex_color,
                'percentage': percentage,
                'count': count
            })

    all_colors.sort(key=lambda x: x['percentage'], reverse=True)

    for i, color in enumerate(all_colors, start=1):
        color['name'] = f'Color {i}'
        color['number'] = i

    # NOTE: we return the ORIGINAL img_array for display,
    # but color stats come from the resized version.
    return img_array, all_colors



def filter_colors(all_colors, total_pixels, exclude_list):
    if not exclude_list:
        # Still recompute percentages from counts so everything is consistent
        total_after = sum(c['count'] for c in all_colors)
        for c in all_colors:
            c['percentage'] = c['count'] / total_after * 100
        return all_colors

    filtered = []
    for color in all_colors:
        if color['number'] not in exclude_list:
            filtered.append(color.copy())

    # Recompute percentages only from the remaining colors
    total_after = sum(c['count'] for c in filtered)
    for c in filtered:
        c['percentage'] = c['count'] / total_after * 100

    return filtered


def create_plot(img_array, colors, exclude_list):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 10))

    # LEFT: Original image
    ax1.imshow(img_array)
    ax1.set_title('Original Image', fontsize=36, fontweight='bold')
    ax1.axis('off')

    # RIGHT: Pie chart
    counts = [c['count'] for c in colors]          # <- use counts as weights
    colors_hex = [c['hex'] for c in colors]
    labels = [c['name'] for c in colors]

    ax2.pie(
        counts,
        labels=labels,
        colors=colors_hex,
        autopct='%1.1f%%',          # Matplotlib now shows count/sum(counts)*100
        startangle=90,
        textprops={'fontsize': 22},
        wedgeprops={'edgecolor': 'black', 'linewidth': 1},
    )

    # Title with excluded info
    if exclude_list:
        excluded_txt = ", ".join(str(n) for n in exclude_list)
        title = f"Filtered Colors (Excluded: {excluded_txt})"
    else:
        title = "All Detected Colors"

    ax2.set_title(title, fontsize=36, fontweight='bold')

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    buf.seek(0)
    img_data = base64.b64encode(buf.getvalue()).decode()
    plt.close(fig)

    return img_data



@app.route('/', methods=['GET', 'POST'])
def upload_or_update():
    # -------------------------
    # 1) GET → show upload page
    # -------------------------
    if request.method == 'GET':
        return render_template('index.html')

    # -------------------------
    # 2) POST: either:
    #   a) initial upload (has file)
    #   b) update exclusions (no file, has exclude + filename)
    # -------------------------

    uploaded_file = request.files.get('file')

    # === CASE A: FIRST ANALYSIS (file uploaded) ===
    if uploaded_file and uploaded_file.filename != '':
        filename = uploaded_file.filename
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        uploaded_file.save(filepath)

        try:
            img_array, all_colors = analyze_image(filepath)
            total_pixels = img_array.shape[0] * img_array.shape[1]

            exclude_list = []  # no exclusions initially
            filtered_colors = filter_colors(all_colors, total_pixels, exclude_list)
            plot_data = create_plot(img_array, filtered_colors, exclude_list)

            return render_template(
                'result.html',
                colors=filtered_colors,
                plot_data=plot_data,
                exclude=exclude_list,
                filename=filename  # needed for later updates
            )
        except Exception as e:
            error_message = f"Error processing image: {str(e)}"
            print(error_message)
            return render_template('index.html', error=error_message)

    # === CASE B: UPDATE EXCLUSIONS (no file, but we get filename + exclude) ===
    filename = request.form.get('filename')
    exclude_raw = request.form.get('exclude', '')

    if not filename:
        # We don't know which image to use → back to index
        return render_template('index.html', error="Missing image reference, please upload again.")

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(filepath):
        return render_template('index.html', error="Image file not found, please upload again.")

    # Parse exclusion list from text like "1,3,7"
    exclude_list = []
    if exclude_raw.strip():
        exclude_list = [int(x) for x in exclude_raw.split(',') if x.strip().isdigit()]

    # Re-analyze using same file
    try:
        img_array, all_colors = analyze_image(filepath)
        total_pixels = img_array.shape[0] * img_array.shape[1]

        filtered_colors = filter_colors(all_colors, total_pixels, exclude_list)
        plot_data = create_plot(img_array, filtered_colors, exclude_list)

        return render_template(
            'result.html',
            colors=filtered_colors,
            plot_data=plot_data,
            exclude=exclude_list,
            filename=filename
        )
    except Exception as e:
        error_message = f"Error updating exclusions: {str(e)}"
        print(error_message)
        return render_template('index.html', error=error_message)


if __name__ == '__main__':
    app.run(debug=True)

# Share Dinkum

**Share Dinkum** is a Django-based application for tracking shares, with a particular focus on Australian-specific tax and accounting considerations, such as franking credits and AMIT cost base adjustments.

Share Dinkum is free and open source.

## Work in Progress

This project is currently under development. Contributions and feedback are welcome.

There may be bugs, and usage is entirely at your own risk. Please refer to the license file for more information.

---

## Setup Instructions

### 1. Clone the Repository

```bash
git clone <repository-url>
cd share-dinkum
```

### 2. Install Dependencies

This project uses [`uv`](https://github.com/astral-sh/uv) for dependency management. Make sure `uv` is installed on your system.

To install dependencies:

```bash
uv pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy and rename the `.env.sample` file to `.env`:

```bash
cp .env.sample .env
```

(Optional) Edit the `.env` file with your environment-specific settings.

By default, the project uses a local SQLite database which requires no additional setup.

### 4. Set Up the Database

Run the following commands to create the database schema:

```bash
python manage.py makemigrations
python manage.py migrate
```

Make sure you are in the same directory as `manage.py`.

### 5. (Optional) Create a Superuser

If you are not using the bulk-import script, create a superuser to access the Django admin:

```bash
python manage.py createsuperuser
```

Note the username and password you create. If using the bulk-import script, update it to reflect the superuser credentials as needed.

### 6. Run the Development Server

Start the development server:

```bash
python manage.py runserver
```

---

## Optional: Data Import Instructions

You can bulk load your share data from Excel using the provided tools.

### 1. Prepare the Data Loading Template

Navigate to the import directory:

```bash
cd share_dinkum_proj/share_dinkum_app/import_data
```

Copy the public template and rename it:

```bash
cp data_import_template_public.xlsx data_import_template_private.xlsx
```

### 2. Edit the Template

Fill in your personal share data in `data_import_template_private.xlsx` using Excel.

### 3. Run the Bulk Load Script

Once your data is ready:

1. Go to the main project directory:

    ```bash
    cd share_dinkum_proj
    ```

2. Open the import notebook:

    ```bash
    jupyter notebook data_import.ipynb
    ```

3. Follow the notebook instructions to load your data into the system.

---

## Contributing

Contributions are welcome. To contribute:

1. Fork the repository.
2. Create a new feature branch: `git checkout -b feature-name`
3. Make your changes and commit: `git commit -m "Describe your changes"`
4. Push your changes: `git push origin feature-name`
5. Open a pull request on GitHub

---

## License

This project is licensed under the GNU Affero General Public License (AGPL). You are free to use, modify, and distribute the software under the terms of the AGPL.

**Limitations of Liability**  
This software is provided "as is" without warranty of any kind, either express or implied. The authors are not liable for any claims or damages resulting from its use.

**Usage at Your Own Risk**  
By using this software, you acknowledge that it is your responsibility to ensure it meets your needs. The authors disclaim responsibility for any losses or issues arising from its use.

For full details, see the `LICENSE` file.

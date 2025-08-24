# Copilot Instructions for Warehouse

## Repository Overview

**Warehouse** is the software that powers PyPI (Python Package Index), the official repository for Python packages. This is a large, production-grade web application serving millions of users.

**Key Technologies:**
- **Backend:** Python 3.13+ with Pyramid web framework, SQLAlchemy ORM, PostgreSQL database
- **Frontend:** JavaScript (ES6), SCSS, Webpack build system, Stimulus controllers
- **Search:** OpenSearch for package indexing and search
- **Caching:** Redis for sessions and data caching
- **Background Tasks:** Celery with Redis broker
- **Development Environment:** Docker Compose with multiple containers
- **File Storage:** Local development, Backblaze B2 and AWS S3 in production

## Build and Development Workflow

### Prerequisites and Setup

**ALWAYS use Docker for development** - the repository is designed for Docker-based development. Direct Python installation is discouraged and unsupported.

**Required tools:**
- Docker (latest version)
- Docker Compose v2
- Make
- ~4GB RAM allocated to Docker (essential for OpenSearch)

### Build Process

**Always run commands in this order:**

1. **Initial build** (run once or when dependencies change):
   ```bash
   make build
   ```
   This builds all Docker containers. Takes 10-15 minutes on first run.

2. **Start development environment:**
   ```bash
   make serve
   ```
   This starts all services and loads test data. Wait for log messages indicating services are ready.

3. **Initialize TUF metadata** (if working with TUF features):
   ```bash
   make inittuf
   ```

**Build artifacts tracking:** The build system uses `.state/` directory to track completion and avoid unnecessary rebuilds. Don't delete these files manually.

### Testing

**Python tests:**
```bash
make tests                           # All Python tests (requires PostgreSQL)
T=tests/unit/test_file.py make tests # Specific test file
TESTARGS="-vvv" make tests          # Pass arguments to pytest
```

**JavaScript tests:**
```bash
make static_tests                   # JavaScript tests with Jest
```

**Linting:**
```bash
make lint                           # Python linting (flake8, black, isort, mypy)
make static_lint                    # JavaScript/CSS linting (eslint, stylelint)
make reformat                       # Auto-format Python code
```

**Coverage:** Python tests automatically generate coverage reports in `htmlcov/`. Coverage must be 100%.

### Common Build Issues and Workarounds

**Database connection timeouts:**
- The `bin/tests` script includes retry logic for PostgreSQL connections
- If tests fail with connection errors, ensure `make serve` is running first

**Docker build SSL certificate errors:**
- Common in CI environments with corporate proxies
- Rebuild individual containers: `docker compose build --no-cache base`

**OpenSearch memory issues:**
- Increase Docker memory allocation to 4GB+
- On Linux, may need: `sysctl -w vm.max_map_count=262144`

**"No space left on device":**
```bash
docker volume rm $(docker volume ls -qf dangling=true)
```

**Full environment reset:**
```bash
make purge                          # Clean all containers and volumes
```

## Project Architecture and Layout

### Core Application Structure

```
warehouse/                          # Main Python package
├── accounts/                       # User authentication and management
├── admin/                          # Admin interface for PyPI administrators  
├── api/                           # REST APIs and upload endpoints
├── classifiers/                   # Package classifier management
├── email/                         # Email sending services
├── forklift/                      # Package upload API implementation
├── manage/                        # Project management interface
├── packaging/                     # Core package/project models and services
├── search/                        # OpenSearch integration
├── static/                        # Frontend assets (JS/SCSS)
├── templates/                     # Jinja2 HTML templates
└── migrations/                    # Database schema migrations
```

### Configuration Files

```
pyproject.toml                     # Python project configuration, pytest, mypy settings
package.json                       # Node.js dependencies and scripts
webpack.config.js                  # Frontend build configuration
docker-compose.yml                 # Development environment services
Makefile                          # Build orchestration
dev/environment                   # Development environment variables
requirements/                     # Python dependencies (pinned with pip-tools)
```

### Frontend Build System

- **Webpack** bundles JavaScript and SCSS
- **Stimulus** controllers for interactive components  
- **Jest** for JavaScript testing
- Assets compiled to `warehouse/static/dist/`

### Key Development Files

```
bin/                              # Shell scripts for common tasks
├── tests                        # Python test runner with coverage
├── lint                         # Python linting pipeline
├── static_tests                 # JavaScript test runner
├── static_lint                  # Frontend linting
├── reformat                     # Code auto-formatting
└── deps                         # Dependency management
```

## CI/CD and Validation

### GitHub Actions Workflows

**Main CI pipeline** (`.github/workflows/ci.yml`):
- Builds Docker images using Depot
- Runs parallel test matrix: Tests, Lint, Documentation, Dependencies, Licenses, Translations
- Requires PostgreSQL, Redis, and Stripe Mock services
- Timeout: 15 minutes

**Node.js CI** (`.github/workflows/node-ci.yml`):
- Static Tests, Static Lint, Static Pipeline
- Uses Node.js 24.4.0 with npm cache

**Critical validation steps:**
1. **Database consistency check** - validates schema migrations
2. **Coverage reporting** - must maintain 100% Python test coverage  
3. **Security scanning** - CodeQL analysis
4. **Dependency scanning** - Dependabot for vulnerabilities

### Pre-commit Validation

Before making changes, always run:
```bash
make build                         # Ensure containers are up to date
make tests                        # Python tests
make static_tests                 # JavaScript tests  
make lint                         # All linting
```

## Development Environment Details

### Docker Services

```yaml
web:          # Main web application (port 80)
worker:       # Celery background tasks
db:           # PostgreSQL 17.5 (port 5433)
redis:        # Redis 7.0 for caching
opensearch:   # Search index
stripe:       # Mock Stripe for billing (port 12111)
static:       # Webpack dev server with live reload (port 35729)
files:        # Local file server (port 9001)
maildev:      # Email testing (port 1080)
```

### Database Operations

```bash
make resetdb                      # Drop and recreate database
make initdb                       # Run migrations and load test data
make dbshell                      # PostgreSQL shell access
make runmigrations               # Run pending migrations only
```

### Working with Test Data

The development environment loads sanitized data from Test PyPI (`dev/example.sql.xz`). This includes sample projects, users, and packages for testing.

### Environment Variables

Development configuration is in `dev/environment`. Key settings:
- Database: `postgresql+psycopg://postgres@db/warehouse`
- Redis: `redis://redis:6379`  
- Search: `http://opensearch:9200/development`
- File storage: Local with HTTP server on port 9001

## Common Development Patterns

### Adding New Features

1. **Database changes:** Create migration in `warehouse/migrations/versions/`
2. **Models:** Add/modify in relevant `warehouse/*/models.py`
3. **Views:** Add to `warehouse/*/views.py` with proper traversal/dispatch
4. **Templates:** Create Jinja2 templates in `warehouse/templates/`
5. **Forms:** Use WTForms in `warehouse/*/forms.py`
6. **Tests:** Add comprehensive unit and functional tests

### Frontend Development

- **Stimulus controllers** in `warehouse/static/js/warehouse/controllers/`
- **SCSS** files follow BEM naming convention
- **Live reload** available on port 35729 during development
- Always run `make static_pipeline` before committing frontend changes

### Testing Strategy

- **Unit tests:** Fast, isolated component tests
- **Functional tests:** Full application integration tests  
- **Browser tests:** Selenium-based UI testing
- Use `pytest.mark.unit` and `pytest.mark.functional` markers

## Important Constraints

- **100% test coverage required** - failing builds will be rejected
- **No direct PostgreSQL dependency installation** - use Docker only
- **Frontend changes require asset compilation** - don't commit source maps
- **Database migrations are irreversible** - test thoroughly
- **Security-sensitive changes** require additional review

## Trust These Instructions

These instructions are comprehensive and regularly validated. Only perform additional exploration if:
1. The instructions are incomplete for your specific task
2. You encounter errors not covered in the troubleshooting sections
3. You need to understand implementation details beyond what's documented

When in doubt, run `make build && make serve && make tests` to verify your environment is working correctly.
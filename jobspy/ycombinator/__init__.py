from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from jobspy.exception import LinkedInException
from jobspy.model import (
    JobPost,
    Location,
    JobResponse,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import create_logger

log = create_logger("YCombinator")

YC_BASE_URL = "https://www.workatastartup.com"
LOGIN_URL = "https://www.workatastartup.com/users/sign_in"


class YCombinator(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.YCOMBINATOR, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        self.driver = None
        self.scraper_input: ScraperInput | None = None

    def _init_driver(self):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        if self.user_agent:
            options.add_argument(f"--user-agent={self.user_agent}")

        service = ChromeService(executable_path=ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)
        return self.driver

    def _ensure_driver(self):
        if self.driver is None:
            self._init_driver()
        return self.driver

    def _perform_login(self, username: str, password: str) -> bool:
        driver = self._ensure_driver()
        driver.get(LOGIN_URL)
        wait = WebDriverWait(driver, 15)
        try:
            email_el = wait.until(EC.presence_of_element_located((By.ID, "user_email")))
            pass_el = driver.find_element(By.ID, "user_password")
            submit = driver.find_element(By.NAME, "commit")
            email_el.clear()
            email_el.send_keys(username)
            pass_el.clear()
            pass_el.send_keys(password)
            submit.click()
            wait.until(EC.url_changes(LOGIN_URL))
            return True
        except Exception as exc:
            log.error(f"YCombinator login failed: {exc}")
            return False

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        username = scraper_input.ycombinator_username
        password = scraper_input.ycombinator_password
        if not username or not password:
            raise ValueError("YCombinator login requires ycombinator_username and ycombinator_password")

        try:
            self._perform_login(username, password)
        except WebDriverException as exc:
            log.error(f"WebDriver failure: {exc}")
            return JobResponse(jobs=[])

        # placeholder: later implement job discovery and detail scraping
        return JobResponse(jobs=[])

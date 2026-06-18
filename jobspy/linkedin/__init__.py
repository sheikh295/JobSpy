from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, urlunparse, unquote

import regex as re
from bs4 import BeautifulSoup
from jobspy.exception import LinkedInException
from jobspy.linkedin.constant import headers
from jobspy.linkedin.util import (
    is_job_remote,
    job_type_code,
    parse_job_type,
    parse_job_level,
    parse_company_industry
)
from jobspy.model import (
    JobPost,
    Location,
    JobResponse,
    Country,
    DescriptionFormat,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import (
    extract_emails_from_text,
    markdown_converter,
    plain_converter,
    create_session,
    remove_attributes,
    create_logger,
)

log = create_logger("LinkedIn")


class LinkedIn(Scraper):
    base_url = "https://www.linkedin.com"
    delay = 3
    band_delay = 4
    jobs_per_page = 25

    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        """
        Initializes LinkedInScraper with the LinkedIn job search url
        """
        super().__init__(Site.LINKEDIN, proxies=proxies, ca_cert=ca_cert)
        self.session = create_session(
            proxies=self.proxies,
            ca_cert=ca_cert,
            is_tls=False,
            has_retry=True,
            delay=5,
            clear_cookies=True,
        )
        self.session.headers.update(headers)
        self.scraper_input = None
        self.country = "worldwide"
        self.job_url_direct_regex = re.compile(r'(?<=\?url=)[^"]+')

    def _log_raw_response_preview(self, *, label: str, response_text: str, url: str):
        """Logs full raw HTML before parsing for debugging."""
        if not response_text:
            log.info(f"{label}: empty response body from {url}")
            return

        body_len = len(response_text)
        log.info(f"{label}: url={url} body_length={body_len}")
        log.info(f"{label} full body start")
        log.info(response_text)
        log.info(f"{label} full body end")

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes LinkedIn for jobs with scraper_input criteria
        :param scraper_input:
        :return: job_response
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []
        seen_ids = set()
        start = scraper_input.offset // 10 * 10 if scraper_input.offset else 0
        request_count = 0
        seconds_old = (
            scraper_input.hours_old * 3600 if scraper_input.hours_old else None
        )

        def continue_search() -> bool:
            return len(job_list) < scraper_input.results_wanted and start < 1000

        while continue_search():
            request_count += 1
            log.info(
                f"search page: {request_count} / {math.ceil(scraper_input.results_wanted / 10)}"
            )
            params = {
                "keywords": scraper_input.search_term,
                "location": scraper_input.location,
                "distance": scraper_input.distance,
                "f_WT": 2 if scraper_input.is_remote else None,
                "f_JT": (
                    job_type_code(scraper_input.job_type)
                    if scraper_input.job_type
                    else None
                ),
                "pageNum": 0,
                "start": start,
                "f_AL": "true" if scraper_input.easy_apply else None,
                "f_C": (
                    ",".join(map(str, scraper_input.linkedin_company_ids))
                    if scraper_input.linkedin_company_ids
                    else None
                ),
            }
            if seconds_old is not None:
                params["f_TPR"] = f"r{seconds_old}"

            params = {k: v for k, v in params.items() if v is not None}
            try:
                response = self.session.get(
                    f"{self.base_url}/jobs-guest/jobs/api/seeMoreJobPostings/search?",
                    params=params,
                    timeout=10,
                )
                if response.status_code not in range(200, 400):
                    if response.status_code == 429:
                        err = "429 Response - Blocked by LinkedIn for too many requests"
                    else:
                        err = f"LinkedIn response status code {response.status_code}"
                        err += f" - {response.text}"
                    log.error(err)
                    return JobResponse(jobs=job_list)
            except Exception as e:
                if "Proxy responded with" in str(e):
                    log.error("LinkedIn: Bad proxy")
                else:
                    log.error(f"LinkedIn: {str(e)}")
                return JobResponse(jobs=job_list)

            self._log_raw_response_preview(
                label="LinkedIn search raw response",
                response_text=response.text,
                url=response.url,
            )

            soup = BeautifulSoup(response.text, "html.parser")
            job_cards = soup.find_all("div", class_="base-search-card")
            if len(job_cards) == 0:
                return JobResponse(jobs=job_list)

            for job_card in job_cards:
                href_tag = job_card.find("a", class_="base-card__full-link")
                if href_tag and "href" in href_tag.attrs:
                    href = href_tag.attrs["href"].split("?")[0]
                    job_id = href.split("-")[-1]

                    if job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    try:
                        job_post = self._process_job_detail(job_id, href)
                        if job_post:
                            job_list.append(job_post)
                        if not continue_search():
                            break
                    except Exception as e:
                        raise LinkedInException(str(e))

            if continue_search():
                time.sleep(random.uniform(self.delay, self.delay + self.band_delay))
                start += len(job_cards)

        job_list = job_list[: scraper_input.results_wanted]
        return JobResponse(jobs=job_list)

    def _process_job_detail(self, job_id: str, job_url: str) -> Optional[JobPost]:
        job_details = self._get_job_details(job_url)
        if not job_details:
            return None

        title = job_details.get("title") or "N/A"
        company = job_details.get("company_name") or "N/A"
        location = job_details.get("location")
        description = job_details.get("description")
        posted_text = job_details.get("posted_text")
        date_posted = self._parse_relative_posted_date(posted_text)

        is_remote = is_job_remote(title, description, location)

        return JobPost(
            id=f"li-{job_id}",
            title=title,
            company_name=company,
            company_url=job_details.get("company_url"),
            location=location,
            is_remote=is_remote,
            date_posted=date_posted,
            job_url=job_details.get("job_url") or job_url,
            compensation=job_details.get("compensation"),
            job_type=job_details.get("job_type"),
            job_level=job_details.get("job_level", "").lower(),
            company_industry=job_details.get("company_industry"),
            description=description,
            job_url_direct=job_details.get("job_url_direct"),
            emails=extract_emails_from_text(description),
            company_logo=job_details.get("company_logo"),
            job_function=job_details.get("job_function"),
        )

    def _get_job_details(self, job_url: str) -> dict:
        """
        Retrieves job description and other job details by going to the job page url
        :param job_page_url:
        :return: dict
        """
        try:
            response = self.session.get(job_url, timeout=5)
            response.raise_for_status()
        except Exception:
            return {}
        if "linkedin.com/signup" in response.url:
            return {}

        self._log_raw_response_preview(
            label="LinkedIn job detail raw response",
            response_text=response.text,
            url=response.url,
        )

        soup = BeautifulSoup(response.text, "html.parser")
        title_tag = soup.find("h1", class_=lambda x: x and "top-card-layout__title" in x)
        company_tag = soup.find("a", class_=lambda x: x and "topcard__org-name-link" in x)
        location_tag = soup.find("span", class_=lambda x: x and "topcard__flavor--bullet" in x)
        posted_tag = soup.find("span", class_=lambda x: x and "posted-time-ago__text" in x)
        div_content = soup.find(
            "div", class_=lambda x: x and "show-more-less-html__markup" in x
        )
        description = None
        if div_content is not None:
            div_content = remove_attributes(div_content)
            description = div_content.prettify(formatter="html")
            if self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
                description = markdown_converter(description)
            elif self.scraper_input.description_format == DescriptionFormat.PLAIN:
                description = plain_converter(description)
        h3_tag = soup.find(
            "h3", text=lambda text: text and "Job function" in text.strip()
        )

        job_function = None
        if h3_tag:
            job_function_span = h3_tag.find_next(
                "span", class_="description__job-criteria-text"
            )
            if job_function_span:
                job_function = job_function_span.text.strip()

        company_logo = (
            logo_image.get("data-delayed-url")
            if (logo_image := soup.find("img", {"class": "artdeco-entity-image"}))
            else None
        )
        return {
            "title": title_tag.get_text(strip=True) if title_tag else None,
            "company_name": company_tag.get_text(strip=True) if company_tag else None,
            "company_url": (
                urlunparse(urlparse(company_tag.get("href"))._replace(query=""))
                if company_tag and company_tag.has_attr("href")
                else None
            ),
            "location": self._get_location_from_string(
                location_tag.get_text(strip=True) if location_tag else None
            ),
            "posted_text": posted_tag.get_text(strip=True) if posted_tag else None,
            "description": description,
            "job_level": parse_job_level(soup),
            "company_industry": parse_company_industry(soup),
            "job_type": parse_job_type(soup) or None,
            "job_url_direct": self._parse_job_url_direct(soup),
            "company_logo": company_logo,
            "job_function": job_function,
            "job_url": self._get_canonical_job_url(soup),
        }

    def _get_canonical_job_url(self, soup: BeautifulSoup) -> str | None:
        canonical_tag = soup.find("link", rel="canonical")
        return canonical_tag.get("href") if canonical_tag and canonical_tag.get("href") else None

    def _get_location_from_string(self, location_string: str | None) -> Location:
        """
        Extracts the location data from the job detail page location string.
        :param location_string
        :return: location
        """
        location = Location(country=Country.from_string(self.country))
        if location_string:
            parts = location_string.split(", ")
            if len(parts) == 2:
                city, state = parts
                location = Location(
                    city=city,
                    state=state,
                    country=Country.from_string(self.country),
                )
            elif len(parts) == 3:
                city, state, country = parts
                country = Country.from_string(country)
                location = Location(city=city, state=state, country=country)
        return location

    def _parse_relative_posted_date(self, posted_text: str | None):
        if not posted_text:
            return None
        text = posted_text.lower().strip()
        now = datetime.now()
        match = re.search(r"(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)", text)
        if not match:
            return now.date()
        value = int(match.group(1))
        unit = match.group(2)
        if "minute" in unit or "hour" in unit:
            return now.date()
        if "day" in unit:
            return (now - timedelta(days=value)).date()
        if "week" in unit:
            return (now - timedelta(weeks=value)).date()
        return now.date()

    def _parse_job_url_direct(self, soup: BeautifulSoup) -> str | None:
        """
        Gets the job url direct from job page
        :param soup:
        :return: str
        """
        job_url_direct = None
        job_url_direct_content = soup.find("code", id="applyUrl")
        if job_url_direct_content:
            job_url_direct_match = self.job_url_direct_regex.search(
                job_url_direct_content.decode_contents().strip()
            )
            if job_url_direct_match:
                job_url_direct = unquote(job_url_direct_match.group())

        return job_url_direct

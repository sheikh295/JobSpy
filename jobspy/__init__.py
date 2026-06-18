from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Tuple
import re

import pandas as pd

from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.model import JobType, Location, JobResponse, Country
from jobspy.model import SalarySource, ScraperInput, Site
from jobspy.util import (
    set_logger_level,
    extract_salary,
    create_logger,
    get_enum_from_value,
    map_str_to_site,
    convert_to_annual,
)
from jobspy.ziprecruiter import ZipRecruiter

RECRUITER_SIGNUP_TEMPLATE = {
    "email": None,
    "password": None,
    "fullName": None,
    "userType": None,
    "phone": None,
    "location": None,
}


def normalize_company_slug(company_url: str | None) -> str | None:
    if not company_url:
        return None
    match = re.search(r"/company/([A-Za-z0-9\-_.]+)/?", company_url)
    return match.group(1).strip(".") if match else None


def infer_region(location: str | None) -> str | None:
    if not location:
        return None
    lower = location.lower()
    if "united states" in lower or re.search(r"\b(us|usa|united states)\b", lower):
        return "US"
    if "canada" in lower:
        return "CA"
    return None


def infer_work_type(is_remote: bool | None, location: str | None, description: str | None) -> str:
    if is_remote:
        return "remote"
    text = f"{location or ''} {description or ''}".lower()
    if "hybrid" in text:
        return "hybrid"
    if "remote" in text or "work from home" in text or "wfh" in text:
        return "remote"
    return "onsite"


def extract_required_skills(skills: str | None) -> list[str]:
    if not skills:
        return []
    if isinstance(skills, list):
        return [skill.strip() for skill in skills if skill and skill.strip()]
    return [skill.strip() for skill in skills.split(",") if skill.strip()]


def extract_requirements(description: str | None) -> list[str]:
    if not description:
        return []
    pieces = re.split(r"\n|\r|\u2022|[-•]", description)
    requirements = [p.strip() for p in pieces if p.strip()]
    return requirements[:10]


def build_tags(title: str | None, company: str | None, industry: str | None) -> list[str]:
    tokens: list[str] = []
    for value in (title, company, industry):
        if not value:
            continue
        for token in re.findall(r"[A-Za-z0-9+#]+", value.lower()):
            if token not in {"and", "for", "the", "with", "at", "in", "of"}:
                tokens.append(token)
    seen = set()
    result: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result[:12]


def build_recommended_record(job: dict) -> dict:
    company_url = job.get("company_url") or job.get("company_url_direct")
    description = job.get("description")
    salary_range = None
    if job.get("min_amount") is not None and job.get("max_amount") is not None:
        salary_range = f"{job.get('currency') or 'USD'} {job.get('min_amount')} - {job.get('max_amount')} {job.get('interval') or ''}".strip()

    return {
        "sourceJobUrl": job.get("job_url") or job.get("job_url_direct"),
        "recruiterSignup": RECRUITER_SIGNUP_TEMPLATE.copy(),
        "companyProfile": {
            "companyName": job.get("company"),
            "companySlug": normalize_company_slug(company_url),
            "industry": job.get("company_industry"),
            "companySize": None,
            "foundedYear": None,
            "website": company_url,
            "logoUrl": job.get("company_logo"),
            "bannerUrl": None,
            "description": job.get("company_description"),
            "tagline": None,
            "headquarters": job.get("location"),
            "location": job.get("location"),
            "locations": [job.get("location")] if job.get("location") else [],
            "linkedinUrl": company_url,
            "twitterUrl": None,
            "facebookUrl": None,
            "contactEmail": None,
            "contactPhone": None,
            "verificationDocument": None,
        },
        "job": {
            "isAnonymous": False,
            "directUrl": job.get("job_url_direct") or job.get("job_url"),
            "title": job.get("title"),
            "highlight": description[:160] if description else None,
            "location": job.get("location"),
            "region": infer_region(job.get("location")),
            "jobType": job.get("job_type"),
            "workType": infer_work_type(job.get("is_remote"), job.get("location"), description),
            "experienceLevel": job.get("job_level") or None,
            "salaryRange": salary_range,
            "currency": job.get("currency") or None,
            "description": description,
            "requiredSkills": extract_required_skills(job.get("skills")),
            "requirements": extract_requirements(description),
            "tags": build_tags(job.get("title"), job.get("company"), job.get("company_industry")),
            "expiresAt": None,
        },
    }


# Update the SCRAPER_MAPPING dictionary in the scrape_jobs function

def scrape_jobs(
    site_name: str | list[str] | Site | list[Site] | None = None,
    search_term: str | None = None,
    google_search_term: str | None = None,
    location: str | None = None,
    distance: int | None = 50,
    is_remote: bool = False,
    job_type: str | None = None,
    easy_apply: bool | None = None,
    results_wanted: int = 15,
    country_indeed: str = "usa",
    proxies: list[str] | str | None = None,
    ca_cert: str | None = None,
    description_format: str = "markdown",
    linkedin_fetch_description: bool | None = False,
    linkedin_company_ids: list[int] | None = None,
    offset: int | None = 0,
    hours_old: int = None,
    enforce_annual_salary: bool = False,
    verbose: int = 0,
    user_agent: str = None,
    **kwargs,
) -> dict:
    """
    Scrapes job data from job boards concurrently
    :return: Recommended schema dictionary containing job items
    """
    SCRAPER_MAPPING = {
        Site.LINKEDIN: LinkedIn,
        Site.INDEED: Indeed,
    }
    set_logger_level(verbose)
    job_type = get_enum_from_value(job_type) if job_type else None

    def get_site_type():
        site_types = list(Site)
        if isinstance(site_name, str):
            site_types = [map_str_to_site(site_name)]
        elif isinstance(site_name, Site):
            site_types = [site_name]
        elif isinstance(site_name, list):
            site_types = [
                map_str_to_site(site) if isinstance(site, str) else site
                for site in site_name
            ]
        return site_types

    country_enum = Country.from_string(country_indeed)

    scraper_input = ScraperInput(
        site_type=get_site_type(),
        country=country_enum,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        distance=distance,
        is_remote=is_remote,
        job_type=job_type,
        easy_apply=easy_apply,
        description_format=description_format,
        linkedin_fetch_description=linkedin_fetch_description,
        results_wanted=results_wanted,
        linkedin_company_ids=linkedin_company_ids,
        offset=offset,
        hours_old=hours_old,
    )

    def scrape_site(site: Site) -> Tuple[str, JobResponse]:
        scraper_class = SCRAPER_MAPPING[site]
        scraper = scraper_class(proxies=proxies, ca_cert=ca_cert, user_agent=user_agent)
        scraped_data: JobResponse = scraper.scrape(scraper_input)
        cap_name = site.value.capitalize()
        site_name = "ZipRecruiter" if cap_name == "Zip_recruiter" else cap_name
        site_name = "LinkedIn" if cap_name == "Linkedin" else cap_name
        create_logger(site_name).info(f"finished scraping")
        return site.value, scraped_data

    site_to_jobs_dict = {}

    def worker(site):
        site_val, scraped_info = scrape_site(site)
        return site_val, scraped_info

    with ThreadPoolExecutor() as executor:
        future_to_site = {
            executor.submit(worker, site): site for site in scraper_input.site_type
        }

        for future in as_completed(future_to_site):
            site_value, scraped_data = future.result()
            site_to_jobs_dict[site_value] = scraped_data

    jobs_dfs: list[pd.DataFrame] = []

    for site, job_response in site_to_jobs_dict.items():
        for job in job_response.jobs:
            job_data = job.dict()
            job_url = job_data["job_url"]
            job_data["site"] = site
            job_data["company"] = job_data["company_name"]
            job_data["job_type"] = (
                ", ".join(job_type.value[0] for job_type in job_data["job_type"])
                if job_data["job_type"]
                else None
            )
            job_data["emails"] = (
                ", ".join(job_data["emails"]) if job_data["emails"] else None
            )
            if job_data["location"]:
                job_data["location"] = Location(
                    **job_data["location"]
                ).display_location()

            # Handle compensation
            compensation_obj = job_data.get("compensation")
            if compensation_obj and isinstance(compensation_obj, dict):
                job_data["interval"] = (
                    compensation_obj.get("interval").value
                    if compensation_obj.get("interval")
                    else None
                )
                job_data["min_amount"] = compensation_obj.get("min_amount")
                job_data["max_amount"] = compensation_obj.get("max_amount")
                job_data["currency"] = compensation_obj.get("currency", "USD")
                job_data["salary_source"] = SalarySource.DIRECT_DATA.value
                if enforce_annual_salary and (
                    job_data["interval"]
                    and job_data["interval"] != "yearly"
                    and job_data["min_amount"]
                    and job_data["max_amount"]
                ):
                    convert_to_annual(job_data)
            else:
                if country_enum == Country.USA:
                    (
                        job_data["interval"],
                        job_data["min_amount"],
                        job_data["max_amount"],
                        job_data["currency"],
                    ) = extract_salary(
                        job_data["description"],
                        enforce_annual_salary=enforce_annual_salary,
                    )
                    job_data["salary_source"] = SalarySource.DESCRIPTION.value

            job_data["salary_source"] = (
                job_data["salary_source"]
                if "min_amount" in job_data and job_data["min_amount"]
                else None
            )

            #naukri-specific fields
            job_data["skills"] = (
                ", ".join(job_data["skills"]) if job_data["skills"] else None
            )
            job_data["experience_range"] = job_data.get("experience_range")
            job_data["company_rating"] = job_data.get("company_rating")
            job_data["company_reviews_count"] = job_data.get("company_reviews_count")
            job_data["vacancy_count"] = job_data.get("vacancy_count")
            job_data["work_from_home_type"] = job_data.get("work_from_home_type")

            job_df = pd.DataFrame([job_data])
            jobs_dfs.append(job_df)

    if jobs_dfs:
        # Step 1: Filter out all-NA columns from each DataFrame before concatenation
        filtered_dfs = [df.dropna(axis=1, how="all") for df in jobs_dfs]

        # Step 2: Concatenate the filtered DataFrames
        jobs_df = pd.concat(filtered_dfs, ignore_index=True)

        job_records = jobs_df.where(pd.notnull(jobs_df), None).to_dict(orient="records")
        recommended_items = [build_recommended_record(job) for job in job_records]
        source_names = sorted({job.get("site") for job in job_records if job.get("site")})
        return {
            "schema": "recommended_schema",
            "source": source_names[0] if len(source_names) == 1 else "jobspy",
            "count": len(recommended_items),
            "items": recommended_items,
        }
    else:
        return {
            "schema": "recommended_schema",
            "source": "jobspy",
            "count": 0,
            "items": [],
        }


__all__ = ["scrape_jobs"]
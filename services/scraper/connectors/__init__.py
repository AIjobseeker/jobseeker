from connectors.greenhouse import GreenhouseConnector
from connectors.lever import LeverConnector
from connectors.ashby import AshbyConnector
from connectors.workday import WorkdayConnector
from connectors.custom.apple import AppleConnector
from connectors.custom.google_careers import GoogleConnector
from connectors.custom.amazon import AmazonConnector
from connectors.base import BaseConnector
from shared.models import ATSType, CompanyConfig


def get_connector(company: CompanyConfig) -> BaseConnector:
    """Factory — returns the right connector for a company's ATS."""
    if company.ats == ATSType.GREENHOUSE:
        return GreenhouseConnector(company)
    if company.ats == ATSType.LEVER:
        return LeverConnector(company)
    if company.ats == ATSType.ASHBY:
        return AshbyConnector(company)
    if company.ats == ATSType.WORKDAY:
        return WorkdayConnector(company)
    if company.ats == ATSType.CUSTOM:
        module = company.custom.scraper_module if company.custom else ""
        if module == "connectors.custom.apple":
            return AppleConnector(company)
        if module == "connectors.custom.google_careers":
            return GoogleConnector(company)
        if module == "connectors.custom.amazon":
            return AmazonConnector(company)
    raise ValueError(f"No connector for {company.name} (ats={company.ats})")

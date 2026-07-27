Create a new and professional RPA for MOADMN Collector. It should be modular and well-documented for reusability, and easier support and expansion in the future.

PREP PHASE:

0.1\. Upon launch, create 'MOADMN Collections' folder in 'Downloads' as the main directory for the RPA. It will contain the subfolders for each run.

0.2\. For each run, a new subfolder will be created under 'MOADMN Collections'. Subfolder for each run must follow the naming convention template 'MOADMN_mm-dd-yyyy_hh-mm-ss-m.s' to ensure unique ID and avoid duplicated filenames on every run.
Use existing filenaming logic already available in finished bot modules.


INITIALIZATION PHASE:

1\. To run automation, user will select date range for documents they will search then click the 'Run' button in the UI. Then it will access the MOADMN court website: https://ahc.mo.gov/case-search-portal-instructions/

Page will load. Click Missouri Administrative Hearing Portal button: //*[@id="custom"]/div/div/div/div/div/div/div/div/div[1]/div/div/div[5]/div/span/a

2\. Page will load. Log in using public account credentials
Login Name: PORTAL
Password: portal1234!

Input Login Name into User ID field: //*[@id="UserID"]
Input Password into Password field: //*[@id="Password"]

Click Sign In button: //*[@id="loginForm"]/form/button

3\. Page will load. Click Matters header link: //*[@id="nav_matters"]

4\. Page will load. Go to Search Filter Field: //*[@id="my-input"]
Enter "25-" or "26-" on the filter field to view all dockets for the year 2025 or 2026

5\. Click Sort By button: //*[@id="sortOptions"]
Clicking Sort By button will show dropdown selection.

Select  Matter ID: //*[@id="sortMenu"]/button[2]

6\. Scroll to the bottom of the page and find the the Items Per Page dropdown:
//*[@id="site-content"]/app-matter-list/section/div/bento-pagination/div/div[1]/div[6]/div/select

Select '100'.

Go through and open each matter ID and go to documents.

To open each matter ID you must click all clickable links. 
Examples below are in XPATH and Element so you can choose which will work better:

<a href="#/matters/matter-overview/238e76e2-ad29-493c-ad2e-bcea73ce639c/" class="">Austin Lewis v. Director of Revenue</a>
or
//*[@id="site-content"]/app-matter-list/section/div/div[1]/div/dl/div[1]/dt/a

<a href="#/matters/matter-overview/399f2da8-2c78-4cbe-952f-02199b756b49/">Clinton James Bruce Salsman v. DIrector of Revenue</a>
or
//*[@id="site-content"]/app-matter-list/section/div/div[2]/div/dl/div[1]/dt/a

<a href="#/matters/matter-overview/0aaf28a9-9a56-49b9-8cba-8004371e8ff6/">White Stone 1 LLC v. Director of Revenue</a>
or
//*[@id="site-content"]/app-matter-list/section/div/div[3]/div/dl/div[1]/dt/a

Once you click and enter a Matter ID, page will load. Then click on/select the Documents tab: //*[@id="tab-Documents"]/span


7\. Input "order" in the Search bar: //*[@id="my-input"]

Then click the Search button: //*[@id="DocumentSearchBtn"]

8\. Extract matter ID, document title, date created to reference for duplicate checking later.

Matter ID usually in this XPath: //*[@id="site-content"]/app-matter-overview/div/dl/div[1]
It starts with "25-", "26-", etc (e.g. '25-0002')

Document Title usually in this XPath: //*[@id="proLawCustomGrid"]/div[1]/div[1]/div[1]/div[3]/div[1]/div/div
Example: 'Order of Dismissal'

Date Created usually in this XPath: //*[@id="proLawCustomGrid"]/div[1]/div[1]/div[1]/div[3]/div[3]/div

8.5\. Once you reach the bottom of the page, check if the Next Page button is clickable: //*[@id="site-content"]/app-matter-list/section/div/bento-pagination/div/div[1]/div[3]/button

If it is, then it means there are more matters to check on the next page. So click it, wait for the page to load and check and extract. Continue until the next button is no longer clickable (which means there is no more matter ID left to check).

Make sure that you only check and collect documents that have 'Order' in their document title. Otherwise, skip and exclude (you can list all the skipped/excluded matters as well as their Document Titles in the report afterwards).

DUPLICATE CHECKING PHASE:

9\. 
#Use existing LNDocTracker duplicate tacker checking module from available bot modules.

'''

Compare the result to the sf-view/lndoc (filename) collected documents using +/- 1 year of the users given date range in the UI.

 	Ex. Matter ID - 12-3456, Doc Title - Order of Dismissal, Date Created - Nov 21, 2025

 		Compare result in sf view with matter id 12-3456 with possible decision date of Nov 14, 2025 - Nov 28, 2025


If document is present in sf-view/lndoc, input "do not collect" *(Optional, depends on python capability)*

If document was not collected, input in the spreadsheet "for collect" *(Optional, depends on python capability)*

'''

CORE AUTOMATION/COLLECTION PHASE:

12\. All documents inputted as for collect, copy the matter id and paste in on the filter located in all matters

13\. Click the link of the result

14\. Click documents

15\. Search "order"

16\. Look for 3 dots and click download

17\. For filenaming convention, use the matter id and date created

Example: 'LDC_SMD_25-1258_12082025_E2E.pdf.00500000JMQMRB.pdf'

Business prefix: 'LDC_SMD'
Matter ID: '25-1258'
Date Created: '12082025' (mmddyyyy date format; normalize if formatted differently)
Business Suffix: 'E2E'


WRAP-UP PHASE:

18\. Reset UI and automation, ready for next run.

19\. After collection and renaming, clean up and finalize run report.

20\. If no new documents are collected show most recent collected document (use Most Recent Document module from exisiting collector bot)

21\. If new documents have been collected, display success prompt. Once user clicks 'OK' automatically open the recent run subfolder so users won't have to manually navigate their directory and hunt for the most recent run subfolder.

22\. Most recent run subfolder will contain the most recent run's log, run report, and the downloaded documents (if available).


*All activity must be logged comprehesively for easier debugging and documentation. Use existing logging logic from available bot modules with summarized data of the run at the end.

*Use PyQt6 for the UI. Utilize already exsiting UI/UX features such as progress bar, sheen animation, completion percentage, and Real-time status messages under the progress bar to inform users the of the backend activity in real-time without the technical jargon.

*UI contains 'Collect' button, 'View Folder' Button', and 'Stop' button (during run so the user can choose to stop the automation when they change their mind). It will also contain the calendar widgets user can use to select their preferred date range. All of these can also be found and referenced from existing modules.

*for comprehensive referencing and resource/module reuse, you can check the modules' original source inside the WVPUC0 Project folder.


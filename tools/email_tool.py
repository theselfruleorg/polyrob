import logging
from typing import Dict, Any, Optional, List, Union
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import imaplib
import email
from email.header import decode_header
import os
import asyncio

from core.config import BotConfig
from core.exceptions import ConfigurationError, APIError, AuthenticationError, ToolError
from tools.base_tool import BaseTool, ToolStatus

class EmailTool(BaseTool):
    """Service for handling email communication."""
    
    # Default email server settings
    DEFAULT_SMTP_SERVER = 'smtp.gmail.com'
    DEFAULT_SMTP_PORT = 587
    DEFAULT_IMAP_SERVER = 'imap.gmail.com'
    
    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management'  # Only need rate limiting for API calls
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {}  # No optional services needed

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize email service."""
        super().__init__(name=name, config=config, container=container)
        
        # Initialize email settings
        self.smtp_server = getattr(config, 'gmail_smtp_server', self.DEFAULT_SMTP_SERVER)
        self.smtp_port = getattr(config, 'gmail_smtp_port', self.DEFAULT_SMTP_PORT)
        self.imap_server = getattr(config, 'gmail_imap_server', self.DEFAULT_IMAP_SERVER)
        
        # Initialize connections
        self.smtp_connection = None
        self.imap_connection = None

    async def _initialize(self) -> None:
        """Initialize email service."""
        try:
            # Validate credentials
            if not all([self.config.gmail_email, self.config.gmail_app_password]):
                self._status = ToolStatus.FAILED
                self._error_message = "Email credentials not configured"
                raise ConfigurationError(self._error_message)
            
            # Test SMTP connection
            try:
                await self._test_smtp_connection()
            except Exception as e:
                self._status = ToolStatus.FAILED
                self._error_message = f"Failed to connect to SMTP server: {e}"
                raise ToolError(self._error_message)

        except Exception as e:
            self._status = ToolStatus.FAILED
            self._error_message = str(e)
            raise ToolError(f"Failed to initialize email service: {e}")

    async def _test_smtp_connection(self) -> None:
        """Test SMTP connection."""
        try:
            context = ssl.create_default_context()
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls(context=context)
            server.login(self.config.gmail_email, self.config.gmail_app_password)
            server.quit()
            self.logger.info("SMTP connection test successful")
        except Exception as e:
            raise ToolError(f"SMTP connection test failed: {e}")

    async def _cleanup(self) -> None:
        """Cleanup email service resources."""
        try:
            # Close SMTP connection
            if self.smtp_connection:
                self.smtp_connection.quit()
                self.smtp_connection = None

            # Close IMAP connection
            if self.imap_connection:
                self.imap_connection.logout()
                self.imap_connection = None

            self.logger.info("Email service cleaned up successfully")

        except Exception as e:
            self.logger.error(f"Error during email service cleanup: {e}")
            raise ToolError(f"Failed to cleanup email service: {e}")

    async def _connect_smtp(self) -> None:
        """Establish SMTP connection."""
        try:
            # Create SSL context
            context = ssl.create_default_context()
            
            # Connect to SMTP server
            self.smtp_connection = smtplib.SMTP(self.smtp_server, self.smtp_port)
            self.smtp_connection.starttls(context=context)
            
            # Login
            self.smtp_connection.login(self.config.gmail_email, self.config.gmail_app_password)
            
        except smtplib.SMTPAuthenticationError as e:
            raise AuthenticationError(f"SMTP authentication failed: {str(e)}")
        except Exception as e:
            raise APIError(f"SMTP connection failed: {str(e)}")

    async def _connect_imap(self) -> None:
        """Establish IMAP connection."""
        try:
            # Connect to IMAP server
            self.imap_connection = imaplib.IMAP4_SSL(self.imap_server)
            
            # Login
            self.imap_connection.login(self.config.gmail_email, self.config.gmail_app_password)
            
        except imaplib.IMAP4.error as e:
            raise AuthenticationError(f"IMAP authentication failed: {str(e)}")
        except Exception as e:
            raise APIError(f"IMAP connection failed: {str(e)}")

    async def send_email(
        self,
        to_email: Union[str, List[str]],
        subject: str,
        body: str,
        html: Optional[str] = None,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None
    ) -> bool:
        """Send an email.
        
        Args:
            to_email: Recipient email address(es)
            subject: Email subject
            body: Plain text email body
            html: Optional HTML version of the email body
            cc: Optional CC recipient(s)
            bcc: Optional BCC recipient(s)
            
        Returns:
            bool: True if email was sent successfully
            
        Raises:
            APIError: If sending fails
        """
        await self.ensure_initialized()
        
        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['From'] = self.config.gmail_email
            msg['Subject'] = subject
            
            # Handle multiple recipients
            if isinstance(to_email, list):
                msg['To'] = ', '.join(to_email)
            else:
                msg['To'] = to_email
                
            # Add CC if provided
            if cc:
                if isinstance(cc, list):
                    msg['Cc'] = ', '.join(cc)
                else:
                    msg['Cc'] = cc
                    
            # Add BCC if provided
            if bcc:
                if isinstance(bcc, list):
                    msg['Bcc'] = ', '.join(bcc)
                else:
                    msg['Bcc'] = bcc

            # Add text body
            msg.attach(MIMEText(body, 'plain'))
            
            # Add HTML version if provided
            if html:
                msg.attach(MIMEText(html, 'html'))

            # Get all recipients
            all_recipients = []
            if isinstance(to_email, list):
                all_recipients.extend(to_email)
            else:
                all_recipients.append(to_email)
                
            if cc:
                if isinstance(cc, list):
                    all_recipients.extend(cc)
                else:
                    all_recipients.append(cc)
                    
            if bcc:
                if isinstance(bcc, list):
                    all_recipients.extend(bcc)
                else:
                    all_recipients.append(bcc)

            # Send email
            if not self.smtp_connection:
                await self._connect_smtp()
                
            self.smtp_connection.send_message(msg)
            
            self.logger.info(f"Email sent successfully to {msg['To']}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to send email: {str(e)}")
            raise APIError(f"Failed to send email: {str(e)}")

    async def read_emails(
        self,
        folder: str = 'INBOX',
        limit: int = 10,
        unread_only: bool = False,
        since_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Read emails from specified folder.
        
        Args:
            folder: Email folder to read from
            limit: Maximum number of emails to return
            unread_only: Only return unread emails
            since_date: Only return emails since this date
            
        Returns:
            List of email dictionaries containing metadata and content
            
        Raises:
            APIError: If reading fails
        """
        await self.ensure_initialized()
        
        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        try:
            if not self.imap_connection:
                await self._connect_imap()
                
            # Select folder
            self.imap_connection.select(folder)
            
            # Build search criteria
            search_criteria = []
            if unread_only:
                search_criteria.append('UNSEEN')
            if since_date:
                date_str = since_date.strftime("%d-%b-%Y")
                search_criteria.append(f'SINCE "{date_str}"')
                
            # Perform search
            if search_criteria:
                _, message_numbers = self.imap_connection.search(None, ' '.join(search_criteria))
            else:
                _, message_numbers = self.imap_connection.search(None, 'ALL')
                
            # Get message numbers and limit results
            message_nums = message_numbers[0].split()
            if limit:
                message_nums = message_nums[-limit:]
                
            emails = []
            for num in message_nums:
                try:
                    _, msg_data = self.imap_connection.fetch(num, '(RFC822)')
                    email_body = msg_data[0][1]
                    email_message = email.message_from_bytes(email_body)
                    
                    # Decode subject
                    subject = decode_header(email_message["Subject"])[0]
                    if isinstance(subject[0], bytes):
                        subject = subject[0].decode(subject[1] or 'utf-8')
                    else:
                        subject = subject[0]
                        
                    # Get sender
                    from_header = decode_header(email_message["From"])[0]
                    if isinstance(from_header[0], bytes):
                        from_addr = from_header[0].decode(from_header[1] or 'utf-8')
                    else:
                        from_addr = from_header[0]
                        
                    # Get date
                    date_str = email_message["Date"]
                    
                    # Get content
                    content = ""
                    html_content = ""
                    
                    if email_message.is_multipart():
                        for part in email_message.walk():
                            if part.get_content_type() == "text/plain":
                                content = part.get_payload(decode=True).decode()
                            elif part.get_content_type() == "text/html":
                                html_content = part.get_payload(decode=True).decode()
                    else:
                        content = email_message.get_payload(decode=True).decode()
                        
                    emails.append({
                        'id': num.decode(),
                        'subject': subject,
                        'from': from_addr,
                        'date': date_str,
                        'content': content,
                        'html_content': html_content
                    })
                    
                except Exception as e:
                    self.logger.error(f"Error processing email {num}: {str(e)}")
                    continue
                    
            return emails

        except Exception as e:
            self.logger.error(f"Failed to read emails: {str(e)}")
            raise APIError(f"Failed to read emails: {str(e)}")

    async def mark_as_read(self, message_id: str, folder: str = 'INBOX') -> bool:
        """Mark an email as read.
        
        Args:
            message_id: Email message ID
            folder: Folder containing the email
            
        Returns:
            bool: True if successful
            
        Raises:
            APIError: If operation fails
        """
        await self.ensure_initialized()
        
        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        try:
            if not self.imap_connection:
                await self._connect_imap()
                
            self.imap_connection.select(folder)
            self.imap_connection.store(message_id.encode(), '+FLAGS', '\\Seen')
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to mark email as read: {str(e)}")
            raise APIError(f"Failed to mark email as read: {str(e)}")

    async def delete_email(self, message_id: str, folder: str = 'INBOX') -> bool:
        """Delete an email.
        
        Args:
            message_id: Email message ID
            folder: Folder containing the email
            
        Returns:
            bool: True if successful
            
        Raises:
            APIError: If deletion fails
        """
        await self.ensure_initialized()
        
        if not self._enabled:
            raise ConfigurationError("Email service is not enabled")

        try:
            if not self.imap_connection:
                await self._connect_imap()
                
            self.imap_connection.select(folder)
            self.imap_connection.store(message_id.encode(), '+FLAGS', '\\Deleted')
            self.imap_connection.expunge()
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to delete email: {str(e)}")
            raise APIError(f"Failed to delete email: {str(e)}")

    async def ensure_initialized(self) -> None:
        """Ensure service is initialized."""
        if not self._initialized:
            await self.initialize() 